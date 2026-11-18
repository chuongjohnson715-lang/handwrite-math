// 在真实浏览器里把「导入背景 → 框选书写区域 → 预览」点一遍。
// 用法：node verify.mjs（会自行启动本地 WebUI，结束时关掉）
import { chromium } from "playwright";
import { spawn, spawnSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, rmSync, unlinkSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import assert from "node:assert/strict";

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = resolve(HERE, "..");
const SHOTS = join(ROOT, "output", "e2e");
const PORT = 8799;
const BASE = `http://127.0.0.1:${PORT}`;
const PYTHON = join(ROOT, ".venv", "Scripts", "python.exe");
// 1×1 米色 PNG：没有现成背景图时也能走完上传流程。
const FALLBACK_PNG = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==",
  "base64",
);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function log(message) {
  console.log(message);
}

async function waitForHealth(timeoutMs = 40000) {
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    try {
      const response = await fetch(`${BASE}/api/health`);
      if (response.ok) return;
    } catch {
      // 还没起来
    }
    await sleep(300);
  }
  throw new Error("WebUI 未能在 40 秒内启动");
}

async function startServer() {
  const child = spawn(PYTHON, ["-m", "markdown_to_handwrite.web", "--port", String(PORT), "--no-browser"], {
    cwd: ROOT,
    stdio: "ignore",
    env: { ...process.env, PYTHONIOENCODING: "utf-8" },
  });
  await waitForHealth();
  return child;
}

async function launchBrowser() {
  try {
    const browser = await chromium.launch({ channel: "msedge", headless: true });
    log("浏览器：本机 Edge");
    return browser;
  } catch (error) {
    log(`本机 Edge 不可用（${error.message.split("\n")[0]}），改用 Playwright 自带 Chromium`);
    return chromium.launch({ headless: true });
  }
}

async function main() {
  mkdirSync(SHOTS, { recursive: true });
  const server = await startServer();
  const browser = await launchBrowser();
  const failures = [];
  const consoleErrors = [];
  const cleanupPaths = new Set();

  try {
    const context = await browser.newContext({ viewport: { width: 1440, height: 960 }, deviceScaleFactor: 1 });
    const page = await context.newPage();

    page.on("console", (message) => {
      if (message.type() !== "error") return;
      const url = message.location()?.url || "";
      if (url.includes("favicon")) return;
      consoleErrors.push(`${message.text()} @ ${url}`);
    });
    page.on("pageerror", (error) => consoleErrors.push(`未捕获异常：${error.message}`));

    const shot = async (name) => {
      await page.screenshot({ path: join(SHOTS, name) });
      log(`  截图 ${name}`);
    };

    // 1. 首页加载
    await page.goto(BASE, { waitUntil: "load" });
    await page.waitForFunction(() => document.querySelectorAll("#example-select option").length > 1, null, {
      timeout: 20000,
    });
    await shot("01-home.png");
    log("1/10 首页加载完成");

    // 2. 导入背景：上传后应自动切到背景素材并打开区域编辑器
    const sourceImage = join(ROOT, "background", "uploads", "演示-实验记录纸.png");
    const uploadBuffer = existsSync(sourceImage) ? readFileSync(sourceImage) : FALLBACK_PNG;
    await page.setInputFiles("#paper-upload", {
      name: "e2e-背景.png",
      mimeType: "image/png",
      buffer: uploadBuffer,
    });
    await page.waitForSelector("#area-modal:not([hidden])", { timeout: 40000 });
    const styleValue = await page.evaluate(() => document.querySelector("#paper-style-select").value);
    assert.equal(styleValue, "image", "导入背景后纸张样式应自动切到“背景素材”");
    const uploadedPath = await page.evaluate(() => state.config.background.image);
    assert.ok(uploadedPath && uploadedPath.startsWith("background/uploads/"), "背景应保存到 background/uploads/");
    cleanupPaths.add(uploadedPath);
    await page.waitForTimeout(800);
    await shot("02-uploaded.png");
    log(`2/10 背景已导入：${uploadedPath}`);

    // 3. 在背景上拖出两个书写区域
    const stage = await page.locator("#area-stage").boundingBox();
    assert.ok(stage && stage.width > 100, "区域编辑器的画布没有正确渲染");
    const dragArea = async (x0, y0, x1, y1) => {
      const startX = stage.x + stage.width * x0;
      const startY = stage.y + stage.height * y0;
      const endX = stage.x + stage.width * x1;
      const endY = stage.y + stage.height * y1;
      await page.mouse.move(startX, startY);
      await page.mouse.down();
      await page.mouse.move((startX + endX) / 2, (startY + endY) / 2, { steps: 6 });
      await page.mouse.move(endX, endY, { steps: 6 });
      await page.mouse.up();
      await page.waitForTimeout(120);
    };
    await dragArea(0.1, 0.12, 0.9, 0.34);
    await dragArea(0.1, 0.55, 0.9, 0.78);
    let boxCount = await page.locator(".area-box").count();
    assert.equal(boxCount, 2, `拖拽应产生 2 个区域，实际 ${boxCount} 个`);

    // 删除单个区域与清空都要真的作用到列表上
    await page.locator(".area-box").first().locator(".area-del").click();
    await page.waitForTimeout(150);
    assert.equal(await page.locator(".area-box").count(), 1, "删除按钮应移除对应区域");
    await page.click("#area-clear");
    await page.waitForTimeout(150);
    assert.equal(await page.locator(".area-box").count(), 0, "清空应移除全部区域");

    // 重新画出两个区域，供后续断言使用
    await dragArea(0.1, 0.12, 0.9, 0.34);
    await dragArea(0.1, 0.55, 0.9, 0.78);
    boxCount = await page.locator(".area-box").count();
    assert.equal(boxCount, 2, `重新拖拽后应有 2 个区域，实际 ${boxCount} 个`);
    await shot("03-areas-drawn.png");
    log("3/10 拖拽、删除、清空均正常，已重新拖出 2 个区域");

    // 4. 应用区域：摘要更新、配置写入、弹窗关闭
    await page.click("#area-apply");
    // 弹窗关闭后元素带 hidden，用可见性等待永远等不到，这里直接等属性状态。
    await page.waitForFunction(() => document.querySelector("#area-modal").hidden === true, null, { timeout: 10000 });
    const summary = (await page.textContent("#area-summary")).trim();
    assert.equal(summary, "2 个区域", `书写区域摘要应为“2 个区域”，实际“${summary}”`);
    const applied = await page.evaluate(() => state.config.background.writing_areas);
    assert.equal(applied.length, 2, "配置里应写入 2 个书写区域");
    for (const area of applied) {
      assert.ok(area.w > 0.5 && area.h > 0.1, `区域尺寸异常：${JSON.stringify(area)}`);
      assert.ok(area.x + area.w <= 1.0001 && area.y + area.h <= 1.0001, `区域越界：${JSON.stringify(area)}`);
    }
    await shot("04-applied.png");
    log(`4/10 区域已应用：${JSON.stringify(applied)}`);

    // 5. 我的字迹：上传样本 → 识别 → 应用
    const fixture = join(HERE, "fixtures", "handwriting-sample.png");
    if (!existsSync(fixture)) {
      spawnSync(PYTHON, [join(HERE, "make_fixture.py")], { cwd: ROOT, stdio: "inherit" });
    }
    await page.setInputFiles("#style-upload", fixture);
    await page.waitForSelector("#style-modal:not([hidden])", { timeout: 90000 });
    const metricCount = await page.locator("#style-metrics .metric").count();
    assert.equal(metricCount, 7, `识别结果应展示 7 项指标，实际 ${metricCount} 项`);
    const suggested = await page.evaluate(() => styleState.result.suggested);
    assert.ok(
      Math.abs(suggested["handwriting.glyph_slant_deg"]) > 4,
      `倾斜应被识别出来，实际 ${suggested["handwriting.glyph_slant_deg"]}°`,
    );
    assert.ok(suggested["handwriting.sdt_stroke_width"] > 15, "笔宽应被识别为偏粗");
    assert.ok(suggested["handwriting.line_spacing"] > 1.5, "行距应被识别为偏松");
    await page.locator("#style-modal .modal-card").screenshot({ path: join(SHOTS, "06-style-result.png") });
    log(`  截图 06-style-result.png（识别结果）`);

    // 保存成手写类型，同时会应用参数并关闭弹窗
    await page.fill("#style-name", "测试笔迹");
    await page.click("#style-save");
    await page.waitForFunction(
      () => document.querySelector("#style-modal").hidden === true
        && document.querySelector("#font-select").value.startsWith("style:"),
      null,
      { timeout: 60000 },
    );
    const appliedStyle = await page.evaluate(() => ({
      slant: state.config.handwriting.glyph_slant_deg,
      stroke: state.config.handwriting.sdt_stroke_width,
      ink: state.config.handwriting.ink_color,
      summary: document.querySelector("#style-summary").textContent,
      selected: document.querySelector("#font-select").value,
      groups: [...document.querySelectorAll("#font-select optgroup")].map((group) => group.label),
      optionText: document.querySelector('#font-select option[value^="style:"]')?.textContent || "",
    }));
    assert.ok(Math.abs(appliedStyle.slant) > 4, "保存后配置里的倾斜应被写入");
    assert.ok(appliedStyle.summary.includes("倾斜"), `左栏应显示已识别的倾斜，实际“${appliedStyle.summary}”`);
    assert.ok(appliedStyle.selected.startsWith("style:profiles/"), `字体下拉应选中手写类型，实际“${appliedStyle.selected}”`);
    assert.ok(
      appliedStyle.groups.includes("我的笔迹（手写类型）"),
      `字体下拉应有“我的笔迹”分组，实际 ${JSON.stringify(appliedStyle.groups)}`,
    );
    assert.ok(appliedStyle.optionText.includes("测试笔迹"), "下拉项应显示笔迹名称");
    await shot("07-style-applied.png");
    log(`5/10 已存为手写类型并选中：${appliedStyle.optionText.trim()}`);

    // 6. 字形库：上传抄写单 → 字体下拉出现"我的字迹" → 正文用真实字形
    const filledSheet = join(HERE, "fixtures", "filled-sheet.png");
    if (!existsSync(filledSheet)) {
      spawnSync(PYTHON, ["-c", "import sys; sys.path.insert(0, 'src'); exec(open('e2e/make_sheet.py').read())"], {
        cwd: ROOT,
        stdio: "inherit",
      });
    }
    await page.setInputFiles("#glyph-upload", filledSheet);
    // 等下拉真正选中，而不是等资源列表更新：后者在前端 setPath 之前就变了。
    await page.waitForFunction(
      () => document.querySelector("#font-select").value.startsWith("glyph:"),
      null,
      { timeout: 180000 },
    );
    const fontState = await page.evaluate(() => ({
      selected: document.querySelector("#font-select").value,
      glyphOptions: [...document.querySelectorAll('option[value^="glyph:"]')].map((option) => option.textContent),
      config: state.config.handwriting.glyph_library,
    }));
    assert.ok(fontState.selected.startsWith("glyph:"), `建库后字体下拉应选中字形库，实际“${fontState.selected}”`);
    assert.ok(fontState.glyphOptions.length >= 1, "字体下拉里应出现字形库选项");
    assert.ok(fontState.config && fontState.config.startsWith("glyphs/"), "配置里应写入字形库路径");
    await shot("09-glyph-library.png");
    log(`6/10 字形库已建好并选中：${fontState.glyphOptions[0]}`);

    // 7. 预览：文字应写进框定区域
    await page.evaluate(() => {
      const range = document.querySelector("#dpi-range");
      range.value = "120";
      range.dispatchEvent(new Event("input", { bubbles: true }));
      const editor = document.querySelector("#markdown-editor");
      editor.value = `# 一、实验目的\n\n${Array.from({ length: 18 }, (_, index) => `第${index + 1}段用于验证书写区域顺序与换页行为。`).join("\n\n")}`;
      editor.dispatchEvent(new Event("input", { bubbles: true }));
      document.querySelector("#editor-wrap").hidden = false;
    });
    await page.click("#render-main");
    await page.waitForFunction(
      () => {
        const image = document.querySelector("#preview-image");
        return !image.hidden && image.naturalWidth > 0 && !state.rendering;
      },
      null,
      { timeout: 180000 },
    );
    const result = await page.evaluate(() => ({
      pages: state.pages.length,
      width: document.querySelector("#preview-image").naturalWidth,
      height: document.querySelector("#preview-image").naturalHeight,
      hint: document.querySelector("#stage-hint").textContent,
    }));
    assert.ok(result.pages >= 1, "预览没有生成任何页面");
    assert.equal(result.width, 992, `120 DPI 下 A4 宽度应为 992px，实际 ${result.width}`);
    assert.ok(result.hint.includes("120 DPI"), `状态栏应显示实际 DPI，实际“${result.hint}”`);
    // 截图前回到页面顶部，并单独截一张完整页面，避免只看到可视区里的中段。
    await page.evaluate(() => {
      const viewport = document.querySelector("#canvas-viewport");
      viewport.scrollTop = 0;
      viewport.scrollLeft = 0;
      window.scrollTo(0, 0);
    });
    await page.waitForTimeout(600);
    await shot("08-preview.png");
    await page.locator("#paper-shell").screenshot({ path: join(SHOTS, "08-preview-page.png") });
    log("  截图 08-preview-page.png（完整页面）");
    log(`7/10 预览完成：${result.pages} 页 · ${result.width}×${result.height} 像素`);

    // 8. 摆正背景：点四个角把斜拍的横线纸拉正，自动检出横线并按行距调字号
    const tilted = join(HERE, "fixtures", "tilted-lines.png");
    if (!existsSync(tilted)) {
      spawnSync(PYTHON, [join(HERE, "make_tilted.py")], { cwd: ROOT, stdio: "inherit" });
    }
    await page.setInputFiles("#paper-upload", tilted);
    await page.waitForSelector("#area-modal:not([hidden])", { timeout: 40000 });
    cleanupPaths.add(await page.evaluate(() => state.config.background.image));
    await page.click("#area-cancel");
    await page.click("#open-rectify");
    await page.waitForFunction(
      () => {
        const image = document.querySelector("#rectify-image");
        return image.complete && image.naturalWidth > 0 && image.getBoundingClientRect().height > 0;
      },
      null,
      { timeout: 30000 },
    );
    const previewBox = await page.locator("#rectify-image").boundingBox();
    // 竖图必须完整落在视口里，底部角点才点得到（历史 bug：62vh 直接裁掉了下半张）。
    assert.ok(previewBox.y + previewBox.height <= 960, `摆正预览被裁切：底边在 ${previewBox.y + previewBox.height}px`);
    const pickCorner = async (rx, ry) => {
      await page.mouse.click(previewBox.x + previewBox.width * rx, previewBox.y + previewBox.height * ry);
      await page.waitForTimeout(80);
    };
    await pickCorner(0.005, 0.02);
    await pickCorner(0.995, 0.033);
    await pickCorner(0.995, 0.993);
    await pickCorner(0.005, 0.98);
    assert.equal((await page.textContent("#rectify-note")).trim(), "已点 4 / 4 个角", "四个角都应被记录");
    await shot("10-rectify-corners.png");

    const fontBefore = await page.evaluate(() => state.config.handwriting.body_font_pt);
    await page.click("#rectify-apply");
    await page.waitForFunction(() => document.querySelector("#rectify-modal").hidden === true, null, {
      timeout: 60000,
    });
    const rectified = await page.evaluate(() => ({
      image: state.config.background.image,
      lines: state.config.background.ruled_lines.length,
      snap: state.config.background.snap_ruled_lines,
      font: state.config.handwriting.body_font_pt,
      message: document.querySelector("#toast-message").textContent,
    }));
    assert.ok(rectified.image.includes("摆正"), `背景应换成摆正后的图，实际 ${rectified.image}`);
    assert.ok(rectified.lines > 10, `摆正后应自动检出横线，实际 ${rectified.lines} 条`);
    assert.equal(rectified.snap, true, "摆正后应开启横线吸附");
    assert.notEqual(rectified.font, fontBefore, "摆正后字号应按行距调整");
    assert.ok(rectified.message.includes("自动检出"), `提示应说明自动检出，实际“${rectified.message}”`);
    cleanupPaths.add(rectified.image);
    // 只改背景不算验证：必须重新渲染，直接量「每行墨迹底边离最近横线多远」。
    await page.click("#render-main");
    await page.waitForFunction(
      () => {
        const image = document.querySelector("#preview-image");
        return !image.hidden && image.naturalWidth > 0 && !state.rendering;
      },
      null,
      { timeout: 180000 },
    );
    const alignment = await page.evaluate(async () => {
      const image = new Image();
      image.src = state.pages[0];
      await image.decode();
      const canvas = document.createElement("canvas");
      canvas.width = image.naturalWidth;
      canvas.height = image.naturalHeight;
      const ctx = canvas.getContext("2d");
      ctx.drawImage(image, 0, 0);
      const { data } = ctx.getImageData(0, 0, canvas.width, canvas.height);
      const inkRows = new Uint32Array(canvas.height);
      for (let y = 0; y < canvas.height; y += 1) {
        let count = 0;
        for (let x = 0; x < canvas.width; x += 1) {
          const i = (y * canvas.width + x) * 4;
          if (data[i] < 110 && data[i + 1] < 110 && data[i + 2] < 150) count += 1;
        }
        inkRows[y] = count;
      }
      const bottoms = [];
      let start = -1;
      for (let y = 0; y < inkRows.length; y += 1) {
        if (inkRows[y] > 12) {
          if (start < 0) start = y;
        } else if (start >= 0) {
          bottoms.push(y - 1);
          start = -1;
        }
      }
      const background = new Image();
      background.src = `/api/background-preview?path=${encodeURIComponent(state.config.background.image)}`;
      await background.decode();
      // 和渲染器一样按 cover 铺满页面，再把图片上的标定线换算到页面像素。
      const scale = Math.max(canvas.width / background.naturalWidth, canvas.height / background.naturalHeight);
      const offsetY = (background.naturalHeight * scale - canvas.height) / 2;
      const rules = state.config.background.ruled_lines.map(
        (value) => value * background.naturalHeight * scale - offsetY,
      );
      const offsets = bottoms.map((bottom) => Math.min(...rules.map((rule) => Math.abs(rule - bottom))));
      return { bottoms, offsets, rules: rules.slice(0, 4), size: [canvas.width, canvas.height] };
    });
    assert.ok(alignment.bottoms.length >= 4, `预览里应有多行文字，实际墨带 ${JSON.stringify(alignment.bottoms)}`);
    const worst = Math.max(...alignment.offsets);
    assert.ok(
      worst <= 7,
      `有文字没压在横线上：最大偏差 ${worst}px（底边 ${JSON.stringify(alignment.bottoms)}，横线 ${JSON.stringify(alignment.rules)}）`,
    );
    await page.evaluate(() => {
      const viewport = document.querySelector("#canvas-viewport");
      viewport.scrollTop = 0;
      viewport.scrollLeft = 0;
      window.scrollTo(0, 0);
    });
    await page.waitForTimeout(400);
    await shot("11-rectified.png");
    log(`  墨迹底边与横线最大偏差 ${worst}px`);
    log(`8/10 背景已摆正：${rectified.lines} 条横线，字号 ${fontBefore} → ${rectified.font} pt`);

    // 9. 手动标定横线：透视翻拍里线距和坡度都渐变，点两条相邻线的四个端点，正文必须逐条跟着斜线落笔
    const perspective = join(HERE, "fixtures", "perspective-lines.png");
    const geometryFile = join(HERE, "fixtures", "perspective-lines.json");
    if (!existsSync(perspective) || !existsSync(geometryFile)) {
      spawnSync(PYTHON, [join(HERE, "make_perspective.py")], { cwd: ROOT, stdio: "inherit" });
    }
    const geometry = JSON.parse(readFileSync(geometryFile, "utf8"));
    await page.setInputFiles("#paper-upload", perspective);
    await page.waitForSelector("#area-modal:not([hidden])", { timeout: 40000 });
    cleanupPaths.add(await page.evaluate(() => state.config.background.image));
    // 书写区域留着会把正文挤进两条带子里，这里清空、改回按页边距整页排版。
    await page.click("#area-clear");
    await page.click("#area-apply");
    await page.waitForFunction(() => document.querySelector("#area-modal").hidden === true, null, { timeout: 10000 });
    await page.evaluate(() => {
      const editor = document.querySelector("#markdown-editor");
      editor.value =
        "天地玄黄宇宙洪荒日月盈昃辰宿列张寒来暑往秋收冬藏闰余成岁律吕调阳云腾致雨露结为霜金生丽水玉出昆冈".repeat(2);
      editor.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await page.click("#open-line-editor");
    await page.waitForFunction(
      () => {
        const image = document.querySelector("#line-image");
        return image.complete && image.naturalWidth > 0 && image.getBoundingClientRect().height > 0;
      },
      null,
      { timeout: 30000 },
    );
    const lineBox = await page.locator("#line-image").boundingBox();
    assert.ok(lineBox.y + lineBox.height <= 960, `标定预览被裁切：底边在 ${lineBox.y + lineBox.height}px`);
    // 点样张记录的两条相邻横线：每条线左右各点一下，坡度就由这两点连出来。
    const anchors = geometry.anchors.map((index) => geometry.lines[index]);
    const rulePoint = (line, rx) => ({
      x: rx,
      y: (line.y + line.slope * (rx * geometry.width - geometry.width / 2)) / geometry.height,
    });
    const pickLine = async (point) => {
      await page.mouse.click(lineBox.x + lineBox.width * point.x, lineBox.y + lineBox.height * point.y);
      await page.waitForTimeout(80);
    };
    await pickLine(rulePoint(anchors[0], 0.15));
    await pickLine(rulePoint(anchors[0], 0.85));
    await pickLine(rulePoint(anchors[1], 0.15));
    await pickLine(rulePoint(anchors[1], 0.85));
    // 四点齐了前端会去识别整页，等它把结果写进提示再往下走。
    await page.waitForFunction(
      () => /识别到整页|整页识别失败|只认出|等距标定/.test(document.querySelector("#line-note").textContent),
      null,
      { timeout: 60000 },
    );
    const lineNote = (await page.textContent("#line-note")).trim();
    assert.ok(/识别到整页 \d+ 条线/.test(lineNote), `应逐条认出整页横线，实际“${lineNote}”`);
    assert.equal(await page.locator("#line-apply").isDisabled(), false, "识别完成后“应用”应可点");
    await shot("12-line-calibration.png");

    await page.click("#line-apply");
    await page.waitForFunction(() => document.querySelector("#line-modal").hidden === true, null, {
      timeout: 20000,
    });
    const calibrated = await page.evaluate(() => ({
      tilt: state.config.background.rule_tilt,
      gradient: state.config.background.rule_tilt_gradient,
      lines: state.config.background.ruled_lines.length,
      snap: state.config.background.snap_ruled_lines,
      areas: state.config.background.writing_areas.length,
      message: document.querySelector("#toast-message").textContent,
    }));
    assert.ok(
      Math.abs(calibrated.tilt - geometry.tilt) < 0.004,
      `标定出的中线坡度应约 ${geometry.tilt}，实际 ${calibrated.tilt}`,
    );
    assert.ok(
      Math.abs(calibrated.gradient - geometry.tilt_gradient) < 2e-5,
      `标定出的坡度变化应约 ${geometry.tilt_gradient}，实际 ${calibrated.gradient}`,
    );
    assert.ok(
      Math.abs(calibrated.lines - geometry.lines.length) <= 2,
      `标定后应认出整页 ${geometry.lines.length} 条线（±2），实际 ${calibrated.lines} 条`,
    );
    assert.equal(calibrated.snap, true, "标定后应开启横线吸附");
    assert.equal(calibrated.areas, 0, "清空后不应再有书写区域");
    assert.ok(
      calibrated.message.includes("整页逐条识别"),
      `提示应说明是逐条识别，实际“${calibrated.message}”`,
    );

    await page.click("#render-main");
    await page.waitForFunction(
      () => {
        const image = document.querySelector("#preview-image");
        return !image.hidden && image.naturalWidth > 0 && !state.rendering;
      },
      null,
      { timeout: 180000 },
    );
    const skewAlignment = await page.evaluate(async (geometry) => {
      const image = new Image();
      image.src = state.pages[0];
      await image.decode();
      const canvas = document.createElement("canvas");
      canvas.width = image.naturalWidth;
      canvas.height = image.naturalHeight;
      const ctx = canvas.getContext("2d");
      ctx.drawImage(image, 0, 0);
      const { data } = ctx.getImageData(0, 0, canvas.width, canvas.height);
      const W = canvas.width;
      const H = canvas.height;
      const background = new Image();
      background.src = `/api/background-preview?path=${encodeURIComponent(state.config.background.image)}`;
      await background.decode();
      const scale = Math.max(W / background.naturalWidth, H / background.naturalHeight);
      const offsetY = (background.naturalHeight * scale - H) / 2;
      const tilt = state.config.background.rule_tilt;
      // 真值取样张自己的几何：每条线在中线处的高度 + 它自己的坡度，不看检测结果。
      const centers = geometry.lines.map((line) => line.y * scale - offsetY);
      const lineAt = (x) => centers.map((center, index) => center + geometry.lines[index].slope * (x - W / 2));
      // 对照模型：整页只用一个坡度（旧行为），越往边上偏得越多。
      const flatAt = (x) => centers.map((center) => center + tilt * (x - W / 2));
      const strips = 10;
      const x0 = W * 0.2;
      const x1 = W * 0.8;
      const follow = [];
      const flat = [];
      for (let s = 0; s < strips; s += 1) {
        const left = Math.round(x0 + ((x1 - x0) * s) / strips);
        const right = Math.round(x0 + ((x1 - x0) * (s + 1)) / strips);
        const width = right - left;
        const rowsWithInk = [];
        for (let y = 0; y < H; y += 1) {
          let count = 0;
          for (let x = left; x < right; x += 1) {
            const i = (y * W + x) * 4;
            if (data[i] < 110 && data[i + 1] < 110 && data[i + 2] < 150) count += 1;
          }
          if (count > 2) rowsWithInk.push(y);
        }
        const spans = [];
        if (rowsWithInk.length) {
          let start = rowsWithInk[0];
          let previous = rowsWithInk[0];
          for (const row of rowsWithInk.slice(1)) {
            if (row > previous + 8) {
              spans.push([start, previous]);
              start = row;
            }
            previous = row;
          }
          spans.push([start, previous]);
        }
        const centerX = (left + right) / 2;
        const local = lineAt(centerX);
        const flatLocal = flatAt(centerX);
        for (const [start, end] of spans) {
          // 只要横贯整条竖带的墨带：半个字、最后一个短行都不算一行。
          let covered = 0;
          for (let x = left; x < right; x += 1) {
            for (let y = start; y <= end; y += 1) {
              const i = (y * W + x) * 4;
              if (data[i] < 110 && data[i + 1] < 110 && data[i + 2] < 150) {
                covered += 1;
                break;
              }
            }
          }
          if (covered < width * 0.5) continue;
          let best = 0;
          let nearest = Infinity;
          for (let index = 0; index < local.length; index += 1) {
            const distance = Math.abs(local[index] - end);
            if (distance < nearest) {
              nearest = distance;
              best = index;
            }
          }
          follow.push(Math.round(nearest));
          flat.push(Math.round(Math.abs(flatLocal[best] - end)));
        }
      }
      return { follow, flat, size: [W, H], tilt };
    }, geometry);
    assert.ok(
      skewAlignment.follow.length >= 20,
      `应量到足够多的墨带，实际 ${skewAlignment.follow.length} 条`,
    );
    const worstSkew = Math.max(...skewAlignment.follow);
    const meanSkew =
      skewAlignment.follow.reduce((sum, value) => sum + value, 0) / skewAlignment.follow.length;
    // 墨带底边本身就带 0~5px 的笔画/抗锯齿抖动；逐条跟随斜线后不该再超出这个量。
    assert.ok(
      worstSkew <= 7 && meanSkew <= 3.5,
      `标定后文字没跟着斜线走：最大偏差 ${worstSkew}px、平均 ${meanSkew.toFixed(1)}px（${JSON.stringify(
        skewAlignment.follow,
      )}）`,
    );
    const worstFlat = Math.max(...skewAlignment.flat);
    // 对照证明坡度变化确实起作用：只用一个坡度时，离中线越远的线偏得越狠。
    assert.ok(
      worstFlat >= worstSkew + 4 && worstFlat >= 7,
      `固定坡度模型应明显偏离，实际最大 ${worstFlat}px（逐条跟随 ${worstSkew}px）`,
    );
    await page.evaluate(() => {
      const viewport = document.querySelector("#canvas-viewport");
      viewport.scrollTop = 0;
      viewport.scrollLeft = 0;
      window.scrollTo(0, 0);
    });
    await page.waitForTimeout(400);
    await shot("13-skewed-lines.png");
    await page.locator("#paper-shell").screenshot({ path: join(SHOTS, "13-skewed-page.png") });
    log(`  墨迹底边与各条斜线最大偏差 ${worstSkew}px（整页单坡度会偏到 ${worstFlat}px）`);
    log(
      `9/10 横线已逐条标定：中线坡度 ${calibrated.tilt}，坡度变化 ${calibrated.gradient}，${calibrated.lines} 条线`,
    );

    // 10. 背景光影：景深只虚远景、光斑只在焦外、暗角只压四周，纸面和文字保持锐利
    const sceneImage = join(HERE, "fixtures", "desk-scene.png");
    if (!existsSync(sceneImage)) {
      spawnSync(PYTHON, [join(HERE, "make_scene.py")], { cwd: ROOT, stdio: "inherit" });
    }
    await page.setInputFiles("#paper-upload", sceneImage);
    await page.waitForSelector("#area-modal:not([hidden])", { timeout: 40000 });
    cleanupPaths.add(await page.evaluate(() => state.config.background.image));
    await dragArea(0.21, 0.16, 0.79, 0.84);
    assert.equal(await page.locator(".area-box").count(), 1, "场景样张上应框出一块书写区域");
    await page.click("#area-apply");
    await page.waitForFunction(() => document.querySelector("#area-modal").hidden === true, null, { timeout: 10000 });

    const setLight = (depth, bokeh, vignette) =>
      page.evaluate(
        ({ depth, bokeh, vignette }) => {
          const values = {
            "background.depth_blur": depth,
            "background.bokeh": bokeh,
            "background.vignette": vignette,
          };
          for (const [path, value] of Object.entries(values)) {
            const range = document.querySelector(`#field-${path.replaceAll(".", "-")}`);
            range.value = String(value);
            range.dispatchEvent(new Event("input", { bubbles: true }));
          }
        },
        { depth, bokeh, vignette },
      );
    const renderScene = async () => {
      await page.click("#render-main");
      await page.waitForFunction(
        () => {
          const image = document.querySelector("#preview-image");
          return !image.hidden && image.naturalWidth > 0 && !state.rendering;
        },
        null,
        { timeout: 180000 },
      );
    };
    // 每次渲染后把像素留在页面里，最后一次性算纸面 / 远景 / 光斑 / 暗角的差别。
    const captureScene = () =>
      page.evaluate(async () => {
        const image = new Image();
        image.src = state.pages[0];
        await image.decode();
        const canvas = document.createElement("canvas");
        canvas.width = image.naturalWidth;
        canvas.height = image.naturalHeight;
        const ctx = canvas.getContext("2d");
        ctx.drawImage(image, 0, 0);
        window.__sceneFrames = window.__sceneFrames || [];
        window.__sceneFrames.push(ctx.getImageData(0, 0, canvas.width, canvas.height));
      });

    await page.evaluate(() => {
      const editor = document.querySelector("#markdown-editor");
      editor.value = "景深测试：这一段文字必须保持锐利，不能被背景的虚化带走。纸面上的横线也要看得清。";
      editor.dispatchEvent(new Event("input", { bubbles: true }));
      window.__sceneFrames = [];
    });
    await setLight(0, 0, 0);
    await renderScene();
    await captureScene();
    await setLight(0.8, 0, 0);
    await renderScene();
    await captureScene();
    await setLight(0.8, 1, 0);
    await renderScene();
    await captureScene();
    await setLight(0.8, 1, 0.8);
    await renderScene();
    await captureScene();

    const scene = await page.evaluate(() => {
      const [off, depthOnly, bokehOnly, lit] = window.__sceneFrames;
      const W = off.width;
      const H = off.height;
      const luma = (frame, x, y) => {
        const i = (y * W + x) * 4;
        const d = frame.data;
        return (d[i] + d[i + 1] + d[i + 2]) / 3;
      };
      const energy = (frame, box) => {
        const [x0, y0, x1, y1] = box;
        let sum = 0;
        let count = 0;
        for (let y = y0; y < y1; y += 1) {
          for (let x = x0; x < x1; x += 1) {
            sum += Math.abs(luma(frame, x, y) - luma(frame, x + 1, y));
            sum += Math.abs(luma(frame, x, y) - luma(frame, x, y + 1));
            count += 2;
          }
        }
        return sum / Math.max(1, count);
      };
      const mean = (frame, box) => {
        const [x0, y0, x1, y1] = box;
        let sum = 0;
        for (let y = y0; y < y1; y += 1) for (let x = x0; x < x1; x += 1) sum += luma(frame, x, y);
        return sum / ((x1 - x0) * (y1 - y0));
      };
      const area = state.config.background.writing_areas[0];
      const paperBox = [
        Math.round(area.x * W) + 10,
        Math.round(area.y * H) + 10,
        Math.round((area.x + area.w) * W) - 10,
        Math.round((area.y + area.h) * H) - 10,
      ];
      // 纸面上边缘留出桌面：光斑和虚化都该发生在这里。
      const farBox = [0, 0, W, Math.round(H * 0.11)];
      const cornerBox = [0, 0, Math.round(W * 0.09), Math.round(H * 0.07)];
      // 光斑单独和「只有景深」那帧比，暗角压暗的光不该算到光斑头上。
      let gained = 0;
      for (let y = farBox[1]; y < farBox[3]; y += 1) {
        for (let x = farBox[0]; x < farBox[2]; x += 1) {
          if (luma(bokehOnly, x, y) - luma(depthOnly, x, y) > 25) gained += 1;
        }
      }
      return {
        paperOff: energy(off, paperBox),
        paperLit: energy(lit, paperBox),
        farOff: energy(off, farBox),
        farLit: energy(lit, farBox),
        cornerOff: mean(off, cornerBox),
        cornerLit: mean(lit, cornerBox),
        gained,
      };
    });
    assert.ok(scene.paperOff > 1.0, `书写区里应有可量的纸面纹理，实际 ${scene.paperOff.toFixed(2)}`);
    assert.ok(
      scene.paperLit >= scene.paperOff * 0.85,
      `纸面和文字被虚化了：${scene.paperOff.toFixed(2)} → ${scene.paperLit.toFixed(2)}`,
    );
    assert.ok(
      scene.farLit <= scene.farOff * 0.65,
      `远景没有虚化：${scene.farOff.toFixed(2)} → ${scene.farLit.toFixed(2)}`,
    );
    assert.ok(scene.gained > 600, `焦外没有亮出光斑，只亮了 ${scene.gained}px`);
    assert.ok(
      scene.cornerLit <= scene.cornerOff * 0.9,
      `暗角没有压暗四周：${scene.cornerOff.toFixed(1)} → ${scene.cornerLit.toFixed(1)}`,
    );
    await page.evaluate(() => {
      const viewport = document.querySelector("#canvas-viewport");
      viewport.scrollTop = 0;
      viewport.scrollLeft = 0;
      window.scrollTo(0, 0);
    });
    await page.waitForTimeout(400);
    await shot("14-scene-light.png");
    await page.locator("#paper-shell").screenshot({ path: join(SHOTS, "14-scene-page.png") });
    log(
      `  纸面 ${scene.paperOff.toFixed(2)} → ${scene.paperLit.toFixed(2)}，远景 ${scene.farOff.toFixed(2)} → ${scene.farLit.toFixed(
        2,
      )}，光斑 ${scene.gained}px`,
    );
    log("10/10 背景光影：远景虚化、光斑、暗角均已生效，纸面保持锐利");

    if (consoleErrors.length) {
      failures.push(`控制台报错 ${consoleErrors.length} 条：\n  - ${consoleErrors.join("\n  - ")}`);
    }
  } catch (error) {
    failures.push(error.message);
    try {
      const pages = browser.contexts()[0]?.pages()[0];
      if (pages) await pages.screenshot({ path: join(SHOTS, "99-failure.png") });
    } catch {
      // 截图失败不影响结论
    }
  } finally {
    await browser.close();
    server.kill();
    for (const path of cleanupPaths) {
      const target = join(ROOT, path);
      try {
        if (existsSync(target)) unlinkSync(target);
      } catch {
        // 清理失败不阻塞结论
      }
    }
    // 测试建的笔迹档案不能留在用户目录里
    try {
      const profile = join(ROOT, "profiles", "测试笔迹");
      if (existsSync(profile)) rmSync(profile, { recursive: true, force: true });
    } catch {
      // 同上
    }
  }

  if (failures.length) {
    console.error(`\n结果：失败 ${failures.length} 项`);
    failures.forEach((item) => console.error(`  - ${item}`));
    process.exit(1);
  }
  log(`\n结果：通过 10 个步骤，控制台报错 0 条`);
  log(`截图目录：${SHOTS}`);
}

main().catch((error) => {
  console.error("验证脚本异常：", error);
  process.exit(1);
});
