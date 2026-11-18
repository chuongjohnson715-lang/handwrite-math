const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

const state = {
  defaults: null,
  config: null,
  sections: [],
  presets: {},
  examples: [],
  assets: { fonts: [], backgrounds: [], glyphLibraries: [], styleProfiles: [] },
  activeStyleProfile: null,
  pages: [],
  page: 0,
  zoom: 1,
  naturalWidth: 0,
  autoFit: true,
  rendering: false,
  renderStartedAt: 0,
  renderTimer: null,
  toastTimer: null,
};

// 缩放基准是预览图的原始像素宽度：100% 即 1:1，屏幕上的笔画像素与 PDF 完全一致。
const ZOOM_MIN = 0.05;
const ZOOM_MAX = 4;
const EMPTY_PAPER_WIDTH = 720;
// 预览生成后先给一个整页总览的比例；要看细节再点 1:1 或放大。
const DEFAULT_ZOOM = 0.25;

// 左栏已有一级控件的字段，高级设置里不再重复。
const PROMOTED_FIELDS = new Set([
  "page.dpi",
  "handwriting.font_path",
  "background.style",
  "background.image",
  "background.writing_areas",
]);

const FONT_LABELS = {
  "1_SDT": "默认 SDT 手写体",
  AnJingChenYingBiXingShu: "暗金陈影行书",
  PingFangShaoHuaTi: "苹方少华体",
};

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

function getPath(object, path) {
  return path.split(".").reduce((current, key) => current?.[key], object);
}

function setPath(object, path, value) {
  const parts = path.split(".");
  const leaf = parts.pop();
  const parent = parts.reduce((current, key) => current[key], object);
  parent[leaf] = value;
}

function mergeDeep(target, source) {
  if (!source || typeof source !== "object" || Array.isArray(source)) return target;
  Object.entries(source).forEach(([key, value]) => {
    if (value && typeof value === "object" && !Array.isArray(value) && target[key]) {
      mergeDeep(target[key], value);
    } else if (key in target) {
      target[key] = value;
    }
  });
  return target;
}

function formatNumber(value, step) {
  if (value === null || value === undefined) return "自动";
  const decimals = String(step).includes(".") ? String(step).split(".")[1].length : 0;
  return Number(value).toFixed(Math.min(decimals, 3)).replace(/\.0+$/, "").replace(/(\.\d*?)0+$/, "$1");
}

function updateRangePaint(input) {
  const min = Number(input.min);
  const max = Number(input.max);
  const value = Number(input.value);
  const progress = max === min ? 0 : ((value - min) / (max - min)) * 100;
  input.style.setProperty("--range-progress", `${Math.max(0, Math.min(100, progress))}%`);
}

// ---- 宏观滑杆：0-100% 映射到多个底层参数，推荐值即引擎出厂默认 ----

const MACRO_FIELDS = {  "macro-ink": (p) => ({
    "handwriting.dry_brush_probability": Math.min(1, Math.round(p * 0.026667 * 100) / 100),
    "handwriting.dry_brush_min_opacity": Math.max(0.1, Math.round((1 - p * 0.0933) * 100) / 100),
  }),
  "macro-position": (p, config) => {
    const basePx = (config.handwriting.body_font_pt / 72) * config.page.dpi * 0.015;
    const round1 = (v) => Math.round(v * 10) / 10;
    const round3 = (v) => Math.round(v * 1000) / 1000;
    return {
      "handwriting.perturb_x_sigma_px": round1((p / 10) * basePx),
      "handwriting.perturb_y_sigma_px": round1((p / 10) * basePx),
      "handwriting.advance_jitter_sigma_ratio": round3(p * 0.001),
      "handwriting.perturb_theta_sigma": round3(p * 0.0008),
      "handwriting.math_perturb_y_sigma_ratio": round3(p * 0.0015),
      "handwriting.elastic_strength_ratio": round3(p * 0.0003),
      "handwriting.baseline_wave_amplitude_ratio": round3(p * 0.0018),
    };
  },
  "macro-stroke": (p) => {
    const round2 = (v) => Math.round(v * 100) / 100;
    return {
      "handwriting.sdt_width_jitter": round2(Math.min(0.35, p * 0.03)),
      "handwriting.sdt_coordinate_jitter": round2(Math.min(2, p * 0.12)),
      "handwriting.ink_density_jitter": round2(Math.min(0.5, p * 0.016)),
      "handwriting.sdt_taper": round2(Math.min(0.6, p * 0.06)),
    };
  },
  "macro-scribble": (p) => ({
    "handwriting.scribble_probability": Math.round(p) / 100,
  }),
};

// 滑杆的推荐值，与引擎出厂默认一一对应。
const MACRO_DEFAULTS = { "macro-ink": 3, "macro-position": 10, "macro-stroke": 5, "macro-scribble": 6 };

// ---- 高级设置里的单参数控件 ----

function fieldShell(meta) {
  const field = document.createElement("div");
  field.className = "config-field";
  field.dataset.search = `${meta.label} ${meta.help || ""} ${meta.path}`.toLowerCase();
  field.dataset.path = meta.path;
  return field;
}

function addFieldHelp(field, meta) {
  if (!meta.help) return;
  const help = document.createElement("p");
  help.className = "recommend";
  help.textContent = meta.help;
  field.append(help);
}

function createRangeControl(meta, nullable = false) {
  const field = fieldShell(meta);
  const currentValue = getPath(state.config, meta.path);
  let workingValue = currentValue ?? Math.max(meta.min, Math.min(meta.max, getPath(state.defaults, meta.path) ?? meta.min));
  const preserveInteger = Number.isInteger(getPath(state.defaults, meta.path)) && Number.isInteger(meta.step);

  const head = document.createElement("div");
  head.className = "field-head";
  const label = document.createElement("label");
  label.className = "field-label";
  label.textContent = meta.label;
  const display = document.createElement("span");
  display.className = "field-value";
  display.textContent = currentValue === null ? "自动" : `${formatNumber(currentValue, meta.step)}${meta.unit || ""}`;
  head.append(label, display);

  const row = document.createElement("div");
  row.className = "range-row";
  const range = document.createElement("input");
  range.type = "range";
  range.min = meta.min;
  range.max = meta.max;
  range.step = meta.step;
  range.value = workingValue;
  range.id = `field-${meta.path.replaceAll(".", "-")}`;
  label.htmlFor = range.id;

  const numberWrap = document.createElement("div");
  numberWrap.className = "number-wrap";
  const number = document.createElement("input");
  number.type = "number";
  number.min = meta.min;
  number.max = meta.max;
  number.step = meta.step;
  number.value = workingValue;
  number.setAttribute("aria-label", `${meta.label}精确值`);
  const unit = document.createElement("span");
  unit.textContent = meta.unit || "";
  numberWrap.append(number, unit);
  row.append(range, numberWrap);

  const commit = (rawValue) => {
    let value = Number(rawValue);
    if (!Number.isFinite(value)) return;
    value = Math.max(meta.min, Math.min(meta.max, value));
    if (preserveInteger) value = Math.round(value);
    workingValue = value;
    setPath(state.config, meta.path, value);
    range.value = value;
    number.value = value;
    display.textContent = `${formatNumber(value, meta.step)}${meta.unit || ""}`;
    updateRangePaint(range);
    markChanged();
  };
  range.addEventListener("input", () => commit(range.value));
  number.addEventListener("change", () => commit(number.value));
  updateRangePaint(range);

  field.append(head, row);
  if (nullable) {
    const autoRow = document.createElement("div");
    autoRow.className = "auto-row";
    const autoLabel = document.createElement("label");
    autoLabel.className = "auto-check";
    const auto = document.createElement("input");
    auto.type = "checkbox";
    auto.checked = currentValue === null;
    autoLabel.append(auto, document.createTextNode("自动计算"));
    autoRow.append(autoLabel);
    const syncAuto = () => {
      const isAuto = auto.checked;
      range.disabled = isAuto;
      number.disabled = isAuto;
      setPath(state.config, meta.path, isAuto ? null : Number(workingValue));
      display.textContent = isAuto ? "自动" : `${formatNumber(workingValue, meta.step)}${meta.unit || ""}`;
      markChanged();
    };
    auto.addEventListener("change", syncAuto);
    range.disabled = auto.checked;
    number.disabled = auto.checked;
    field.append(autoRow);
  }
  addFieldHelp(field, meta);
  return field;
}

function createToggleControl(meta) {
  const field = fieldShell(meta);
  const row = document.createElement("div");
  row.className = "toggle-row";
  const copy = document.createElement("div");
  const label = document.createElement("label");
  label.className = "field-label";
  label.textContent = meta.label;
  copy.append(label);
  if (meta.help) {
    const help = document.createElement("p");
    help.className = "recommend";
    help.textContent = meta.help;
    copy.append(help);
  }
  const switchLabel = document.createElement("label");
  switchLabel.className = "switch";
  const input = document.createElement("input");
  input.type = "checkbox";
  input.checked = Boolean(getPath(state.config, meta.path));
  input.id = `field-${meta.path.replaceAll(".", "-")}`;
  label.htmlFor = input.id;
  const track = document.createElement("span");
  track.className = "switch-track";
  switchLabel.append(input, track);
  input.addEventListener("change", () => {
    setPath(state.config, meta.path, input.checked);
    markChanged();
  });
  row.append(copy, switchLabel);
  field.append(row);
  return field;
}

function createSelectControl(meta) {
  const field = fieldShell(meta);
  const label = document.createElement("label");
  label.className = "field-label";
  label.textContent = meta.label;
  const select = document.createElement("select");
  select.className = "select";
  select.id = `field-${meta.path.replaceAll(".", "-")}`;
  label.htmlFor = select.id;
  meta.options.forEach((option) => {
    const element = document.createElement("option");
    element.value = option.value;
    element.textContent = option.label;
    select.append(element);
  });
  select.value = getPath(state.config, meta.path);
  select.addEventListener("change", () => {
    setPath(state.config, meta.path, select.value);
    if (meta.path === "background.style") {
      if (select.value !== "image") setPath(state.config, "background.image", null);
      syncPaperControls();
    }
    markChanged();
  });
  field.append(label, select);
  addFieldHelp(field, meta);
  return field;
}

function createColorControl(meta) {
  const field = fieldShell(meta);
  const label = document.createElement("label");
  label.className = "field-label";
  label.textContent = meta.label;
  const row = document.createElement("div");
  row.className = "color-row";
  const picker = document.createElement("input");
  picker.type = "color";
  picker.value = getPath(state.config, meta.path);
  picker.setAttribute("aria-label", `${meta.label}选择器`);
  const text = document.createElement("input");
  text.type = "text";
  text.value = getPath(state.config, meta.path);
  text.setAttribute("aria-label", `${meta.label}色值`);
  picker.addEventListener("input", () => {
    text.value = picker.value;
    setPath(state.config, meta.path, picker.value);
    markChanged();
  });
  text.addEventListener("change", () => {
    if (/^#[0-9a-f]{6}$/i.test(text.value)) picker.value = text.value;
    setPath(state.config, meta.path, text.value.trim());
    markChanged();
  });
  row.append(picker, text);
  field.append(label, row);
  addFieldHelp(field, meta);
  return field;
}

function createAssetPathControl(meta) {
  const field = fieldShell(meta);
  const label = document.createElement("label");
  label.className = "field-label";
  label.textContent = meta.label;
  const isFont = meta.control === "font-path";
  const assets = isFont ? state.assets.fonts : state.assets.backgrounds;
  const autoLabel = isFont ? "自动（按 font 目录顺序）" : "自动（按 background 目录顺序）";
  const CUSTOM = "__custom__";
  const currentValue = getPath(state.config, meta.path) ?? "";

  const select = document.createElement("select");
  select.className = "select";
  select.id = `field-${meta.path.replaceAll(".", "-")}`;
  label.htmlFor = select.id;

  const autoOption = document.createElement("option");
  autoOption.value = "";
  autoOption.textContent = autoLabel;
  select.append(autoOption);

  assets.forEach((asset) => {
    const option = document.createElement("option");
    option.value = asset.value;
    option.textContent = asset.label;
    select.append(option);
  });

  // 当前值不在内置资源里（自定义路径），补一个临时选项，避免丢失已有配置
  const knownValues = new Set(["", ...assets.map((asset) => asset.value)]);
  if (currentValue && !knownValues.has(currentValue)) {
    const customOption = document.createElement("option");
    customOption.value = currentValue;
    customOption.textContent = currentValue;
    select.append(customOption);
  }

  const customPick = document.createElement("option");
  customPick.value = CUSTOM;
  customPick.textContent = "自定义路径…";
  select.append(customPick);

  select.value = currentValue || "";

  const customInput = document.createElement("input");
  customInput.className = "text-control";
  customInput.type = "text";
  customInput.placeholder = isFont ? "填写字体路径，如 font/我的字体.ttf" : "填写背景图路径，如 background/paper.jpg";
  customInput.hidden = select.value !== CUSTOM;
  customInput.value = select.value === CUSTOM ? "" : (currentValue && !knownValues.has(currentValue) ? currentValue : "");

  select.addEventListener("change", () => {
    if (select.value === CUSTOM) {
      customInput.hidden = false;
      customInput.focus();
      return;
    }
    customInput.hidden = true;
    setPath(state.config, meta.path, select.value === "" ? null : select.value);
    if (meta.path === "background.style") syncPaperControls();
    if (meta.path === "background.image" && select.value) {
      setPath(state.config, "background.style", "image");
      syncPrimaryControls();
    }
    markChanged();
  });

  customInput.addEventListener("change", () => {
    const value = customInput.value.trim();
    if (!value) {
      customInput.hidden = true;
      select.value = "";
      setPath(state.config, meta.path, null);
    } else {
      let matched = Array.from(select.options).find((option) => option.value === value);
      if (!matched) {
        matched = document.createElement("option");
        matched.value = value;
        matched.textContent = value;
        select.insertBefore(matched, customPick);
      }
      select.value = value;
      setPath(state.config, meta.path, value);
    }
    markChanged();
  });

  field.append(label, select, customInput);
  addFieldHelp(field, meta);
  return field;
}

function createTextControl(meta) {
  const field = fieldShell(meta);
  const label = document.createElement("label");
  label.className = "field-label";
  label.textContent = meta.label;
  const input = document.createElement("input");
  input.className = "text-control";
  input.type = "text";
  input.value = getPath(state.config, meta.path) ?? "";
  input.id = `field-${meta.path.replaceAll(".", "-")}`;
  label.htmlFor = input.id;
  field.append(label, input);
  input.addEventListener("change", () => {
    const value = input.value.trim();
    setPath(state.config, meta.path, value === "" && meta.path !== "handwriting.seed" ? null : value);
    markChanged();
  });
  addFieldHelp(field, meta);
  return field;
}

function createWritingAreaControl(meta) {
  const field = fieldShell(meta);
  const head = document.createElement("div");
  head.className = "field-head";
  const label = document.createElement("span");
  label.className = "field-label";
  label.textContent = meta.label;
  const value = document.createElement("span");
  value.className = "field-value";
  value.dataset.areaSummary = "inline";
  value.textContent = areaSummaryText();
  head.append(label, value);
  const button = document.createElement("button");
  button.className = "btn btn-quiet compact block";
  button.type = "button";
  button.textContent = "设定书写区域";
  button.addEventListener("click", () => openAreaEditor());
  field.append(head, button);
  addFieldHelp(field, meta);
  return field;
}

function createSectionImagesControl(meta) {
  const field = fieldShell(meta);
  const head = document.createElement("div");
  head.className = "field-head";
  const label = document.createElement("span");
  label.className = "field-label";
  label.textContent = meta.label;
  const value = document.createElement("span");
  value.className = "field-value";
  value.dataset.sectionSummary = "inline";
  value.textContent = sectionSummaryText();
  head.append(label, value);
  const button = document.createElement("button");
  button.className = "btn btn-quiet compact block";
  button.type = "button";
  button.textContent = "指定分节背景";
  button.addEventListener("click", () => openSectionEditor());
  field.append(head, button);
  addFieldHelp(field, meta);
  return field;
}

function createField(meta) {
  if (meta.control === "range") return createRangeControl(meta);
  if (meta.control === "nullable-range") return createRangeControl(meta, true);
  if (meta.control === "toggle") return createToggleControl(meta);
  if (meta.control === "select") return createSelectControl(meta);
  if (meta.control === "color") return createColorControl(meta);
  if (meta.control === "font-path" || meta.control === "background-path") return createAssetPathControl(meta);
  if (meta.control === "writing-areas") return createWritingAreaControl(meta);
  if (meta.control === "section-images") return createSectionImagesControl(meta);
  if (meta.control === "ruled-lines") return createRuledLinesControl(meta);
  return createTextControl(meta);
}

function createRuledLinesControl(meta) {
  const field = document.createElement("div");
  field.className = "field";
  const head = document.createElement("div");
  head.className = "field-head";
  const label = document.createElement("label");
  label.className = "field-label";
  label.textContent = meta.label;
  const value = document.createElement("span");
  value.className = "field-value";
  const lines = getPath(state.config, meta.path) || [];
  value.textContent = lines.length ? `${lines.length} 条` : "未检测";
  head.append(label, value);
  field.append(head);
  if (meta.help) {
    const note = document.createElement("p");
    note.className = "panel-note";
    note.textContent = meta.help;
    field.append(note);
  }
  return field;
}

function renderConfigSections() {
  const container = $("#config-sections");
  const openGroups = new Set(
    $$(".group", container).filter((group) => group.open).map((group) => group.dataset.section),
  );
  container.replaceChildren();
  let fieldCount = 0;
  state.sections.forEach((section) => {
    const fieldsMeta = section.fields.filter((meta) => !PROMOTED_FIELDS.has(meta.path));
    if (!fieldsMeta.length) return;
    const group = document.createElement("details");
    group.className = "group";
    group.dataset.section = section.id;
    group.dataset.search = `${section.title} ${section.description || ""} ${section.eyebrow || ""}`.toLowerCase();
    group.open = openGroups.size ? openGroups.has(section.id) : false;
    const summary = document.createElement("summary");
    const name = document.createElement("span");
    name.className = "group-name";
    const title = document.createElement("strong");
    title.textContent = section.title;
    name.append(title);
    if (section.description) {
      const description = document.createElement("small");
      description.textContent = section.description;
      name.append(description);
    }
    const count = document.createElement("span");
    count.className = "group-count";
    count.textContent = `${fieldsMeta.length} 项`;
    summary.append(name, count);
    const fields = document.createElement("div");
    fields.className = "group-fields";
    fieldsMeta.forEach((meta) => fields.append(createField(meta)));
    fieldCount += fieldsMeta.length;
    group.append(summary, fields);
    container.append(group);
  });
  $("#field-count").textContent = fieldCount;
  applyConfigSearch();
  syncAreaSummary();
}

function applyConfigSearch() {
  const query = $("#config-search").value.trim().toLowerCase();
  let visibleGroups = 0;
  $$(".group", $("#config-sections")).forEach((group) => {
    let visibleFields = 0;
    $$(".config-field", group).forEach((field) => {
      const visible = !query || field.dataset.search.includes(query) || group.dataset.search.includes(query);
      field.hidden = !visible;
      if (visible) visibleFields += 1;
    });
    group.hidden = visibleFields === 0;
    if (visibleFields > 0) visibleGroups += 1;
    if (query && visibleFields > 0) group.open = true;
  });
  const oldEmpty = $(".no-results", $("#config-sections"));
  if (oldEmpty) oldEmpty.remove();
  if (!visibleGroups) {
    const empty = document.createElement("div");
    empty.className = "no-results";
    empty.textContent = "没有找到匹配的参数";
    $("#config-sections").append(empty);
  }
}

function markChanged() {
  $("#save-state").textContent = "有未生成的更改";
  $("#save-state").style.color = "var(--accent)";
  $$("[data-preset]").forEach((button) => button.classList.remove("active"));
}

// ---- 左栏一级控件 ----

function fontLabel(asset) {
  const stem = asset.value.replace(/^.*\//, "").replace(/\.(ttf|otf|ttc)$/i, "").replace(/-2$/, "");
  return FONT_LABELS[stem] || stem;
}

function fillSelect(select, items, currentValue, autoLabel) {
  select.replaceChildren();
  const autoOption = document.createElement("option");
  autoOption.value = "";
  autoOption.textContent = autoLabel;
  select.append(autoOption);
  items.forEach((item) => {
    const option = document.createElement("option");
    option.value = item.value;
    option.textContent = item.label;
    select.append(option);
  });
  const known = new Set(["", ...items.map((item) => item.value)]);
  if (currentValue && !known.has(currentValue)) {
    const option = document.createElement("option");
    option.value = currentValue;
    option.textContent = currentValue;
    select.append(option);
  }
  select.value = currentValue || "";
}

function syncPaperControls() {
  const style = state.config.background.style;
  $("#paper-style-select").value = style;
  const imageSelect = $("#paper-image-select");
  imageSelect.hidden = style !== "image";
  if (style === "image") {
    fillSelect(
      imageSelect,
      state.assets.backgrounds.map((asset) => ({ value: asset.value, label: asset.label })),
      state.config.background.image,
      "自动（按 background 目录顺序）",
    );
  }
}

function syncFontSelect() {
  const select = $("#font-select");
  select.replaceChildren();
  const auto = document.createElement("option");
  auto.value = "";
  auto.textContent = "自动（按 font 目录顺序）";
  select.append(auto);

  const fontGroup = document.createElement("optgroup");
  fontGroup.label = "内置字体";
  state.assets.fonts.forEach((asset) => {
    const option = document.createElement("option");
    option.value = `font:${asset.value}`;
    option.textContent = fontLabel(asset);
    fontGroup.append(option);
  });
  select.append(fontGroup);

  const libraries = state.assets.glyphLibraries || [];
  if (libraries.length) {
    const group = document.createElement("optgroup");
    group.label = "我的字迹（字形库）";
    libraries.forEach((library) => {
      const option = document.createElement("option");
      option.value = `glyph:${library.value}`;
      const percent = Math.round((library.coverage || 0) * 100);
      option.textContent = `${library.label}（${library.count} 字 · 常用字覆盖 ${percent}%）`;
      group.append(option);
    });
    select.append(group);
  }

  const profiles = state.assets.styleProfiles || [];
  if (profiles.length) {
    const group = document.createElement("optgroup");
    group.label = "我的笔迹（手写类型）";
    profiles.forEach((profile) => {
      const option = document.createElement("option");
      option.value = `style:${profile.value}`;
      option.textContent = `${profile.label}（倾斜 ${profile.style["handwriting.glyph_slant_deg"] ?? 0}° · 笔宽 ${profile.style["handwriting.sdt_stroke_width"] ?? "—"}）`;
      group.append(option);
    });
    select.append(group);
  }

  const current = state.activeStyleProfile
    ? `style:${state.activeStyleProfile}`
    : state.config.handwriting.glyph_library
      ? `glyph:${state.config.handwriting.glyph_library}`
      : state.config.handwriting.font_path
        ? `font:${state.config.handwriting.font_path}`
        : "";
  select.value = current;
}

function syncPrimaryControls() {
  syncFontSelect();
  syncPaperControls();
  const dpi = state.config.page.dpi ?? 300;
  const dpiRange = $("#dpi-range");
  dpiRange.value = dpi;
  $("#dpi-value").textContent = `${dpi} DPI`;
  updateRangePaint(dpiRange);
  Object.keys(MACRO_FIELDS).forEach((id) => {
    const slider = $(`#${id}`);
    $(`#${id}-value`).textContent = `${slider.value}%`;
    updateRangePaint(slider);
  });
  syncAreaSummary();
}

// ---- 背景导入 ----

function readFileAsDataUrl(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ""));
    reader.onerror = () => reject(new Error("文件读取失败，请重试。"));
    reader.readAsDataURL(file);
  });
}

async function importBackground(file) {
  if (!file) return;
  const chip = $("#paper-upload-label");
  const text = $("#paper-upload-text");
  const original = text.textContent;
  chip.classList.add("busy");
  text.textContent = "正在导入…";
  try {
    const data = await readFileAsDataUrl(file);
    const response = await fetch("/api/upload-background", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ filename: file.name, data }),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "导入失败");
    const asset = result.asset;
    state.assets.backgrounds = [
      { label: asset.label, value: asset.path },
      ...state.assets.backgrounds.filter((item) => item.value !== asset.path),
    ];
    setPath(state.config, "background.image", asset.path);
    setPath(state.config, "background.style", "image");
    const dropped = clearLineCalibration();
    syncPaperControls();
    markChanged();
    showToast(
      "背景已导入",
      dropped
        ? `${asset.label} · 换了背景，之前的横线标定已作废，请重新标定并框出正文区域。`
        : `${asset.label} · 接下来框出正文可以书写的部分。`,
    );
    openAreaEditor();
  } catch (error) {
    showToast("背景导入失败", error.message, true);
  } finally {
    chip.classList.remove("busy");
    text.textContent = original;
  }
}

function clearLineCalibration() {
  const lines = state.config.background?.ruled_lines || [];
  if (!lines.length && !state.config.background?.snap_ruled_lines) return false;
  setPath(state.config, "background.ruled_lines", []);
  setPath(state.config, "background.rule_tilt", 0);
  setPath(state.config, "background.rule_tilt_gradient", 0);
  setPath(state.config, "background.snap_ruled_lines", false);
  return true;
}

// ---- 我的字迹 ----
const styleState = { result: null };

function downloadTemplate() {
  const name = state.template?.name || "common-300";
  const link = document.createElement("a");
  link.href = `/api/glyph-template/${encodeURIComponent(name)}-1.png`;
  link.download = `抄写模板-${name}.png`;
  link.click();
  showToast("模板已开始下载", "打印后用平时写字的笔照抄，一个字写一格，写在格子里即可。");
}

async function buildGlyphLibrary(files) {
  const chip = $("#glyph-upload-label");
  chip.classList.add("busy");
  try {
    const pages = [];
    for (const file of files) pages.push(await readFileAsDataUrl(file));
    const response = await fetch("/api/build-glyphs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pages, name: "我的字迹" }),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "建库失败");
    state.assets.glyphLibraries = result.libraries || [];
    const library = result.library;
    // 建库后要切到字形库：若不清掉"当前笔迹档"，下拉会继续显示那个档案。
    state.activeStyleProfile = null;
    setPath(state.config, "handwriting.glyph_library", library.path);
    syncFontSelect();
    markChanged();
    showToast(
      "字形库已建好",
      `${library.count} 个字，常用字覆盖 ${Math.round((library.coverage || 0) * 100)}%。字体下拉里已选中它。`,
    );
  } catch (error) {
    showToast("建库失败", error.message, true);
  } finally {
    chip.classList.remove("busy");
  }
}

async function analyzeStyleSample(file) {
  if (!file) return;
  const chip = $("#style-upload-label");
  const text = $("#style-upload-text");
  const original = text.textContent;
  chip.classList.add("busy");
  text.textContent = "正在识别…";
  try {
    const data = await readFileAsDataUrl(file);
    const response = await fetch("/api/analyze-handwriting", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ data }),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "识别失败");
    styleState.result = result;
    styleState.sampleData = data;
    renderStyleResult(result);
    $("#style-modal").hidden = false;
  } catch (error) {
    showToast("字迹识别失败", error.message, true);
  } finally {
    chip.classList.remove("busy");
    text.textContent = original;
  }
}

function metricRow(name, value, swatch) {
  const row = document.createElement("div");
  row.className = "metric";
  const label = document.createElement("span");
  label.className = "metric-name";
  label.textContent = name;
  const display = document.createElement("span");
  display.className = "metric-value";
  if (swatch) {
    const dot = document.createElement("span");
    dot.className = "metric-swatch";
    dot.style.background = swatch;
    display.append(dot);
  }
  display.append(document.createTextNode(value));
  row.append(label, display);
  return row;
}

function renderStyleResult(result) {
  const metrics = result.metrics || {};
  const suggested = result.suggested || {};
  const overlay = $("#style-overlay");
  if (result.overlay) {
    overlay.src = result.overlay;
    overlay.hidden = false;
  } else {
    overlay.removeAttribute("src");
    overlay.hidden = true;
  }

  const percent = (value) => `${Math.round((Number(value) || 0) * 100)}%`;
  const pick = (key, suffix) => `${suggested[`handwriting.${key}`] ?? "—"}${suffix}`;
  $("#style-metrics").replaceChildren(
    metricRow("样本质量", percent(metrics.quality)),
    metricRow("识别内容", `${metrics.rows || 0} 行 · ${metrics.characters || 0} 字`),
    metricRow("笔宽", pick("sdt_stroke_width", " / 22")),
    metricRow("整体倾斜", pick("glyph_slant_deg", "°")),
    metricRow("行距", pick("line_spacing", " ×")),
    metricRow("墨色", suggested["handwriting.ink_color"] || "—", suggested["handwriting.ink_color"]),
    metricRow("规整度", percent(metrics.regularity)),
  );

  $("#style-note").textContent = [...(result.notes || []), ...(result.warnings || [])].join(" ");
  const quality = $("#style-quality");
  quality.textContent = result.usable ? "识别完成，可以应用" : "样本不足，建议重拍后再试";
  quality.classList.toggle("warn", !result.usable);
  $("#style-apply").disabled = !result.usable;
}

function closeStyleModal() {
  $("#style-modal").hidden = true;
}

function applyStyleValues(result) {
  Object.entries(result.suggested).forEach(([path, value]) => setPath(state.config, path, value));
  syncPrimaryControls();
  renderConfigSections();
  markChanged();
  $("#style-summary").textContent = `倾斜 ${result.suggested["handwriting.glyph_slant_deg"]}°`;
}

function applyStyleResult() {
  const result = styleState.result;
  if (!result || !result.usable) return;
  applyStyleValues(result);
  state.activeStyleProfile = null;
  syncFontSelect();
  closeStyleModal();
  showToast("已应用字迹参数", "笔宽、倾斜、行距和墨色已写入配置，可直接预览。");
}

async function saveStyleProfile() {
  const result = styleState.result;
  if (!result || !result.usable) return;
  const name = ($("#style-name").value || "").trim() || "我的笔迹";
  try {
    const response = await fetch("/api/save-style", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name,
        data: styleState.sampleData,
        suggested: result.suggested,
        metrics: result.metrics,
        glyphLibrary: state.config.handwriting.glyph_library || null,
      }),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "保存失败");
    state.assets.styleProfiles = payload.profiles || [];
    applyStyleValues(result);
    state.activeStyleProfile = payload.profile.path;
    syncFontSelect();
    closeStyleModal();
    showToast("已存为手写类型", `“${payload.profile.name}”已加入字体下拉，下次直接选它。`);
  } catch (error) {
    showToast("保存失败", error.message, true);
  }
}

async function detectRuledLines() {
  const path = state.config.background.image;
  if (!path) {
    showToast("先选一张横线纸背景", "“检测横线”读取的是当前背景图。", true);
    return;
  }
  const button = $("#detect-lines");
  button.classList.add("busy");
  try {
    const response = await fetch("/api/detect-lines", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path }),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "检测失败");
    if (!result.usable) {
      showToast(
        "没找到规整的横线",
        `只检测到 ${result.count} 条线，间距一致性 ${Math.round((result.confidence || 0) * 100)}%。建议用“设定书写区域”手动框选。`,
        true,
      );
      return;
    }
    setPath(state.config, "background.ruled_lines", result.positions);
    setPath(state.config, "background.rule_tilt", 0);
    setPath(state.config, "background.rule_tilt_gradient", 0);
    setPath(state.config, "background.snap_ruled_lines", true);
    renderConfigSections();
    markChanged();
    const pitchMm = (result.pitch * (state.config.page.height_mm || 297)).toFixed(1);
    showToast("已对齐横线", `检测到 ${result.count} 条横线，间距约 ${pitchMm} mm；行高已量化到线间距。`);
  } catch (error) {
    showToast("检测失败", error.message, true);
  } finally {
    button.classList.remove("busy");
  }
}

// ---- 摆正背景 ----

const rectifyState = { picks: [] };

function openRectify() {
  const path = state.config.background.image;
  if (!path) {
    showToast("先选一张背景图", "摆正读取的是当前背景。", true);
    return;
  }
  rectifyState.picks = [];
  const image = $("#rectify-image");
  image.src = `/api/background-preview?path=${encodeURIComponent(path)}`;
  image.onload = drawRectifyOverlay;
  $("#rectify-modal").hidden = false;
  $("#rectify-summary").textContent = "";
  drawRectifyOverlay();
}

function closeRectify() {
  $("#rectify-modal").hidden = true;
}

function drawRectifyOverlay() {
  const stage = $("#rectify-stage");
  const canvas = $("#rectify-canvas");
  const note = $("#rectify-note");
  const apply = $("#rectify-apply");
  const width = stage.clientWidth;
  const height = stage.clientHeight;
  if (!width || !height) return;
  canvas.width = width;
  canvas.height = height;
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, width, height);

  const points = rectifyState.picks.map(([x, y]) => [x * width, y * height]);
  if (points.length > 1) {
    ctx.strokeStyle = "rgba(0, 122, 255, 0.9)";
    ctx.lineWidth = 2;
    ctx.beginPath();
    points.forEach(([x, y], index) => (index ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
    if (points.length === 4) ctx.closePath();
    ctx.stroke();
  }
  points.forEach(([x, y], index) => {
    ctx.fillStyle = "#007aff";
    ctx.beginPath();
    ctx.arc(x, y, 5, 0, Math.PI * 2);
    ctx.fill();
    ctx.fillStyle = "#fff";
    ctx.font = "600 11px -apple-system, sans-serif";
    ctx.fillText(String(index + 1), x + 8, y - 8);
  });

  note.textContent = `已点 ${points.length} / 4 个角`;
  apply.disabled = points.length !== 4;
}

function handleRectifyClick(event) {
  // 以图片本身为基准：舞台在图片外可能留白，用舞台矩形会让坐标整体偏移。
  const rect = $("#rectify-image").getBoundingClientRect();
  const x = (event.clientX - rect.left) / rect.width;
  const y = (event.clientY - rect.top) / rect.height;
  if (x < 0 || x > 1 || y < 0 || y > 1) return;
  if (rectifyState.picks.length >= 4) rectifyState.picks = [];
  rectifyState.picks.push([x, y]);
  drawRectifyOverlay();
}

async function applyRectify() {
  const corners = rectifyState.picks;
  if (corners.length !== 4) return;
  const apply = $("#rectify-apply");
  apply.classList.add("busy");
  try {
    const response = await fetch("/api/rectify-background", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: state.config.background.image, corners }),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "摆正失败");
    const fixed = result.rectified;

    setPath(state.config, "background.style", "image");
    setPath(state.config, "background.image", fixed.path);
    state.assets.backgrounds = result.backgrounds || state.assets.backgrounds;
    // 背景换了，旧标定作废；新图能自动检出横线就直接用上，并按行距把字号调好。
    setPath(state.config, "background.ruled_lines", fixed.lines.usable ? fixed.lines.positions : []);
    // 摆正后横线是水平的，旧的倾斜量必须清掉。
    setPath(state.config, "background.rule_tilt", 0);
    setPath(state.config, "background.rule_tilt_gradient", 0);
    setPath(state.config, "background.snap_ruled_lines", Boolean(fixed.lines.usable));
    let resizedPt = null;
    if (fixed.lines.usable) {
      const suggested = fontPtForLinePitch(fixed.lines.pitch * (state.config.page.height_mm || 297));
      const current = state.config.handwriting.body_font_pt || 16;
      if (Math.abs(suggested - current) / current > 0.15) {
        setPath(state.config, "handwriting.body_font_pt", suggested);
        resizedPt = suggested;
      }
    }
    syncPrimaryControls();
    renderConfigSections();
    markChanged();
    closeRectify();
    showToast(
      "背景已摆正",
      fixed.lines.usable
        ? `已切到摆正后的图，自动检出 ${fixed.lines.count} 条横线${resizedPt ? `；字号已按行距调到 ${resizedPt} pt` : ""}。`
        : "已切到摆正后的图；横线请用“手动标定横线”点两条横线的左右两端。",
    );
  } catch (error) {
    showToast("摆正失败", error.message, true);
  } finally {
    apply.classList.remove("busy");
  }
}

// ---- 横线标定 ----
// 实拍照片里的横线往往是斜的：只量间距的话，正文在左右两侧会离开横线。
// 因此每条横线都点左右两端（两条相邻线共四个点），渲染时整行跟着斜率走。
const LINE_HINTS = [
  "已点 0 / 4 个点：点第一条横线的左端",
  "已点 1 / 4 个点：点这条横线的右端",
  "已点 2 / 4 个点：点下一条横线的左端",
  "已点 3 / 4 个点：点这条横线的右端",
];

// detected: 这组预览线是从图里逐条认出来的（而不是按线距等距外推）。
const lineState = { picks: [], preview: [], slope: 0, pitch: 0, tiltGradient: 0, detected: false, token: 0 };

function openLineEditor() {
  const path = state.config.background.image;
  if (!path) {
    showToast("先选一张横线纸背景", "标定读取的是当前背景图。", true);
    return;
  }
  lineState.picks = [];
  lineState.preview = [];
  lineState.slope = 0;
  lineState.pitch = 0;
  lineState.tiltGradient = 0;
  lineState.detected = false;
  lineState.token += 1;
  const image = $("#line-image");
  image.onload = updateLinePreview;
  image.src = `/api/background-preview?path=${encodeURIComponent(path)}`;
  $("#line-note").textContent = LINE_HINTS[0];
  $("#line-summary").textContent = "";
  $("#line-apply").disabled = true;
  $("#line-modal").hidden = false;
  updateLinePreview();
}

function closeLineEditor() {
  $("#line-modal").hidden = true;
}

function lineGeometry() {
  const picks = lineState.picks;
  const image = $("#line-image");
  const imageWidth = image.naturalWidth;
  const imageHeight = image.naturalHeight;
  if (picks.length < 4 || !imageWidth || !imageHeight) return null;
  const [first, second, third, fourth] = picks;
  const dxUpper = (second.x - first.x) * imageWidth;
  const dxLower = (fourth.x - third.x) * imageWidth;
  if (Math.abs(dxUpper) < imageWidth * 0.15 || Math.abs(dxLower) < imageWidth * 0.15) {
    return { error: "每条横线都要点在左右两端，两个点再分开一些" };
  }
  const upperSlope = ((second.y - first.y) * imageHeight) / dxUpper;
  const lowerSlope = ((fourth.y - third.y) * imageHeight) / dxLower;
  if (Math.abs(upperSlope - lowerSlope) > 0.06) {
    return { error: "两次点的倾斜差太多，请重新点相邻的两条横线" };
  }
  const slope = (upperSlope + lowerSlope) / 2;
  if (Math.abs(slope) > 0.4) {
    return { error: "这条线太歪了，请确认点的是横线" };
  }
  // 线高统一折算到图片中线，和渲染器的页面中线约定一致。
  const centerY = (point, ownSlope) =>
    point.y + ((ownSlope * imageWidth) / imageHeight) * (0.5 - point.x);
  const upper = centerY(first, upperSlope);
  const lower = centerY(third, lowerSlope);
  const pitch = Math.abs(lower - upper);
  if (pitch < 0.004) {
    return { error: "两条横线挨得太近，请点相邻的两条" };
  }
  const top = Math.min(upper, lower);
  const bottom = Math.max(upper, lower);
  const positions = [];
  for (let y = top; y > 0.005; y -= pitch) positions.unshift(y);
  for (let y = bottom; y < 0.995; y += pitch) positions.push(y);
  return { slope, pitch, positions };
}

function linePitchMm(pitchRatio) {
  // 背景按 cover 铺满页面；宽图会裁掉左右，所以线距要用同一套变换换算成毫米。
  const image = $("#line-image");
  const { width_mm = 210, height_mm = 297, dpi = 300 } = state.config.page;
  if (!image.naturalWidth || !image.naturalHeight) return pitchRatio * height_mm;
  const scale = Math.max(
    (width_mm / 25.4 * dpi) / image.naturalWidth,
    (height_mm / 25.4 * dpi) / image.naturalHeight,
  );
  return (pitchRatio * image.naturalHeight * scale) / dpi * 25.4;
}

function drawLineMarks() {
  const stage = $("#line-stage");
  const canvas = $("#line-marks");
  const width = stage.clientWidth;
  const height = stage.clientHeight;
  if (!width || !height) return;
  if (canvas.width !== width) canvas.width = width;
  if (canvas.height !== height) canvas.height = height;
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, width, height);
  const image = $("#line-image");
  const picks = lineState.picks;

  if (lineState.preview.length && image.naturalWidth) {
    // 透视照片里坡度随高度变化：每条预览线按自己所在的高度取坡度。
    const aspect = image.naturalWidth / image.naturalHeight;
    ctx.strokeStyle = "rgba(255, 59, 48, 0.55)";
    ctx.lineWidth = 1.5;
    lineState.preview.forEach((centerY) => {
      const slope = lineState.slope + lineState.tiltGradient * image.naturalHeight * (centerY - 0.5);
      const normalizedSlope = slope * aspect;
      ctx.beginPath();
      ctx.moveTo(0, (centerY - normalizedSlope / 2) * height);
      ctx.lineTo(width, (centerY + normalizedSlope / 2) * height);
      ctx.stroke();
    });
  }

  ctx.strokeStyle = "rgba(0, 122, 255, 0.9)";
  ctx.lineWidth = 2;
  for (const [start, end] of [[0, 1], [2, 3]]) {
    if (picks.length <= end) continue;
    ctx.beginPath();
    ctx.moveTo(picks[start].x * width, picks[start].y * height);
    ctx.lineTo(picks[end].x * width, picks[end].y * height);
    ctx.stroke();
  }

  picks.forEach((point, index) => {
    const x = point.x * width;
    const y = point.y * height;
    ctx.fillStyle = "#007aff";
    ctx.beginPath();
    ctx.arc(x, y, 4, 0, Math.PI * 2);
    ctx.fill();
    ctx.fillStyle = "#fff";
    ctx.font = "600 11px -apple-system, sans-serif";
    ctx.fillText(String(index + 1), x + 7, y - 7);
  });
}

function updateLinePreview() {
  const token = ++lineState.token;
  const geometry = lineGeometry();
  const note = $("#line-note");
  const apply = $("#line-apply");
  const valid = geometry && !geometry.error;
  lineState.preview = valid ? geometry.positions : [];
  lineState.slope = valid ? geometry.slope : 0;
  lineState.pitch = valid ? geometry.pitch : 0;
  lineState.tiltGradient = 0;
  lineState.detected = false;
  drawLineMarks();

  if (lineState.picks.length < 4) {
    note.textContent = LINE_HINTS[Math.min(lineState.picks.length, 3)];
    apply.disabled = true;
    return;
  }
  if (!valid) {
    note.textContent = geometry ? geometry.error : "背景图还没加载好，请重试";
    apply.disabled = true;
    return;
  }
  const pitchMm = linePitchMm(geometry.pitch);
  const tiltDeg = (Math.atan(geometry.slope) * 180) / Math.PI;
  note.textContent = `间距 ${pitchMm.toFixed(1)} mm，倾斜 ${tiltDeg.toFixed(1)}°，共 ${geometry.positions.length} 条线（按等距外推）`;
  $("#line-summary").textContent = "确认后开启对齐";
  apply.disabled = false;
  detectLineGrid(token);
}

async function detectLineGrid(token) {
  // 照片有透视：线距和坡度从上到下都在变，等距外推会越写越偏。
  // 拿点中的两条线做锚点，去图里把整页每一条线都找出来。
  const picks = lineState.picks.map((point) => [Number(point.x.toFixed(5)), Number(point.y.toFixed(5))]);
  const note = $("#line-note");
  const apply = $("#line-apply");
  note.textContent = "正在识别整页横线…";
  apply.disabled = true;
  try {
    const response = await fetch("/api/detect-lines", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: state.config.background.image, points: picks }),
    });
    const result = await response.json();
    if (token !== lineState.token) return;
    if (!response.ok) throw new Error(result.error || "识别失败");
    if (!result.usable) {
      note.textContent = `只认出 ${result.count} 条线，退回等距标定；确认后正文按线距吸附。`;
      apply.disabled = false;
      return;
    }
    lineState.preview = result.positions;
    lineState.slope = result.tilt;
    lineState.pitch = result.pitch;
    lineState.tiltGradient = result.tilt_gradient || 0;
    lineState.detected = true;
    drawLineMarks();
    const pitchMm = linePitchMm(result.pitch);
    const tiltDeg = (Math.atan(result.tilt) * 180) / Math.PI;
    note.textContent = `识别到整页 ${result.count} 条线，间距 ${pitchMm.toFixed(1)} mm，倾斜 ${tiltDeg.toFixed(1)}°（透视已校正）`;
    apply.disabled = false;
  } catch (error) {
    if (token !== lineState.token) return;
    note.textContent = `整页识别失败：${error.message}。确认后按等距标定。`;
    apply.disabled = false;
  }
}

function handleLineClick(event) {
  // 以图片本身为基准：舞台带一圈边框，用舞台矩形会让坐标整体偏移。
  const rect = $("#line-image").getBoundingClientRect();
  const x = (event.clientX - rect.left) / rect.width;
  const y = (event.clientY - rect.top) / rect.height;
  if (x < 0 || x > 1 || y < 0 || y > 1) return;
  if (lineState.picks.length >= 4) lineState.picks = [];
  lineState.picks.push({ x, y });
  updateLinePreview();
}

function fontPtForLinePitch(pitchMm) {
  // 手写字高通常占行距的六七成；不跟着调字号的话，字会缩在格子一角。
  return Math.max(8, Math.min(40, Math.round(pitchMm * 2.0 * 10) / 10));
}

function applyLineCalibration() {
  const positions = lineState.preview;
  if (positions.length < 3) return;
  const pitchMm = linePitchMm(lineState.pitch);
  const tiltDeg = (Math.atan(lineState.slope) * 180) / Math.PI;
  const suggestedPt = fontPtForLinePitch(pitchMm);
  const currentPt = state.config.handwriting.body_font_pt || 16;
  const shouldResize = Math.abs(suggestedPt - currentPt) / currentPt > 0.15;
  const tilted = Math.abs(lineState.slope) >= 0.002;

  setPath(state.config, "background.ruled_lines", positions.map((value) => Number(value.toFixed(5))));
  setPath(state.config, "background.rule_tilt", Number(lineState.slope.toFixed(6)));
  setPath(state.config, "background.rule_tilt_gradient", Number(lineState.tiltGradient.toFixed(8)));
  setPath(state.config, "background.snap_ruled_lines", true);
  if (shouldResize) setPath(state.config, "handwriting.body_font_pt", suggestedPt);
  renderConfigSections();
  syncPrimaryControls();
  markChanged();
  closeLineEditor();
  showToast(
    "横线已标定",
    `${lineState.detected ? "整页逐条识别，" : ""}共 ${positions.length} 条线，间距 ${pitchMm.toFixed(1)} mm${
      tilted ? `，倾斜 ${tiltDeg.toFixed(1)}°` : ""
    }${shouldResize ? `；字号已按行距调到 ${suggestedPt} pt。` : "；行高已量化到线间距。"}`,
  );
}

// ---- 书写区域编辑器 ----
// 区域坐标统一用页面宽高的比例表示，渲染时再换算成像素，因此换纸张尺寸也不会错位。
const AREA_MIN = 0.02;
const areaState = { areas: [], mode: null, index: -1, selected: -1, start: null, origin: null };

function clamp01(value) {
  return Math.max(0, Math.min(1, value));
}

function round4(value) {
  return Math.round(value * 10000) / 10000;
}

function areaSummaryText() {
  const areas = state.config?.background?.writing_areas || [];
  return areas.length ? `${areas.length} 个区域` : "整页（按页边距）";
}

function syncAreaSummary() {
  const text = areaSummaryText();
  const primary = $("#area-summary");
  if (primary) primary.textContent = text;
  $$('[data-area-summary="inline"]').forEach((node) => { node.textContent = text; });
}

function sectionSummaryText() {
  const entries = state.config?.background?.section_images || [];
  return entries.length ? `${entries.length} 节` : "未指定";
}

function syncSectionSummary() {
  const text = sectionSummaryText();
  $$('[data-section-summary="inline"]').forEach((node) => { node.textContent = text; });
}

function marginArea() {
  const page = state.config.page;
  const width = page.width_mm || 210;
  const height = page.height_mm || 297;
  const left = (page.margin_left_mm || 0) / width;
  const right = (page.margin_right_mm || 0) / width;
  const top = (page.margin_top_mm || 0) / height;
  const bottom = (page.margin_bottom_mm || 0) / height;
  return [{
    x: round4(left),
    y: round4(top),
    w: round4(Math.max(AREA_MIN, 1 - left - right)),
    h: round4(Math.max(AREA_MIN, 1 - top - bottom)),
  }];
}

function syncAreaStage() {
  const stage = $("#area-stage");
  const image = $("#area-image");
  const config = state.config;
  const width = config.page.width_mm || 210;
  const height = config.page.height_mm || 297;
  stage.style.aspectRatio = `${width} / ${height}`;
  stage.style.setProperty("--paper-preview", config.background.paper_color || "#fffdf4");
  const note = $("#area-note");
  const useImage = config.background.style === "image" && config.background.image;
  image.hidden = !useImage;
  if (useImage) {
    image.onerror = () => {
      image.hidden = true;
      note.textContent = "背景预览加载失败，仍可按纸张比例框选区域。";
    };
    image.src = `/api/background-preview?path=${encodeURIComponent(config.background.image)}`;
    note.textContent = "拖拽空白处新建区域；拖动框体移动，拖动右下角调整大小。";
  } else {
    image.removeAttribute("src");
    note.textContent = "当前纸张由程序绘制，这里只显示底色；拖拽即可框选书写区域。";
  }
}

function paintAreaBoxes() {
  $$(".area-box", $("#area-layer")).forEach((box) => {
    const area = areaState.areas[Number(box.dataset.index)];
    if (!area) return;
    box.style.left = `${area.x * 100}%`;
    box.style.top = `${area.y * 100}%`;
    box.style.width = `${area.w * 100}%`;
    box.style.height = `${area.h * 100}%`;
    box.classList.toggle("selected", Number(box.dataset.index) === areaState.selected);
  });
}

function renderAreaBoxes() {
  const layer = $("#area-layer");
  layer.replaceChildren();
  areaState.areas.forEach((area, index) => {
    const box = document.createElement("div");
    box.className = "area-box";
    box.dataset.index = String(index);
    const badge = document.createElement("span");
    badge.className = "area-badge";
    badge.textContent = String(index + 1);
    const remove = document.createElement("button");
    remove.className = "area-del";
    remove.type = "button";
    remove.textContent = "×";
    remove.title = `删除区域 ${index + 1}`;
    remove.addEventListener("pointerdown", (event) => event.stopPropagation());
    remove.addEventListener("click", (event) => {
      event.stopPropagation();
      areaState.areas.splice(index, 1);
      areaState.selected = -1;
      renderAreaBoxes();
    });
    const handle = document.createElement("span");
    handle.className = "area-handle";
    box.append(badge, remove, handle);
    layer.append(box);
  });
  paintAreaBoxes();
}

function stagePoint(event) {
  const rect = $("#area-stage").getBoundingClientRect();
  if (!rect.width || !rect.height) return { x: 0, y: 0 };
  return {
    x: clamp01((event.clientX - rect.left) / rect.width),
    y: clamp01((event.clientY - rect.top) / rect.height),
  };
}

function normalizeArea(start, end) {
  return {
    x: Math.min(start.x, end.x),
    y: Math.min(start.y, end.y),
    w: Math.abs(end.x - start.x),
    h: Math.abs(end.y - start.y),
  };
}

function bindAreaStage() {
  const layer = $("#area-layer");

  layer.addEventListener("pointerdown", (event) => {
    if (event.button !== 0) return;
    const box = event.target.closest(".area-box");
    const point = stagePoint(event);
    if (box) {
      const index = Number(box.dataset.index);
      areaState.mode = event.target.classList.contains("area-handle") ? "resize" : "move";
      areaState.index = index;
      areaState.selected = index;
      areaState.start = point;
      areaState.origin = { ...areaState.areas[index] };
    } else {
      areaState.areas.push({ x: point.x, y: point.y, w: 0, h: 0 });
      areaState.mode = "create";
      areaState.index = areaState.areas.length - 1;
      areaState.selected = areaState.index;
      areaState.start = point;
      areaState.origin = null;
      renderAreaBoxes();
    }
    layer.setPointerCapture(event.pointerId);
    event.preventDefault();
    paintAreaBoxes();
  });

  layer.addEventListener("pointermove", (event) => {
    if (!areaState.mode) return;
    const area = areaState.areas[areaState.index];
    if (!area) return;
    const point = stagePoint(event);
    if (areaState.mode === "create") {
      Object.assign(area, normalizeArea(areaState.start, point));
    } else if (areaState.mode === "move" && areaState.origin) {
      const origin = areaState.origin;
      area.x = Math.max(0, Math.min(1 - origin.w, origin.x + point.x - areaState.start.x));
      area.y = Math.max(0, Math.min(1 - origin.h, origin.y + point.y - areaState.start.y));
      area.w = origin.w;
      area.h = origin.h;
    } else if (areaState.mode === "resize" && areaState.origin) {
      const origin = areaState.origin;
      area.x = origin.x;
      area.y = origin.y;
      area.w = Math.max(AREA_MIN, point.x - origin.x);
      area.h = Math.max(AREA_MIN, point.y - origin.y);
    }
    paintAreaBoxes();
  });

  const finish = (event) => {
    if (!areaState.mode) return;
    const area = areaState.areas[areaState.index];
    if (areaState.mode === "create" && area && (area.w < AREA_MIN || area.h < AREA_MIN)) {
      areaState.areas.splice(areaState.index, 1);
      areaState.selected = -1;
    }
    areaState.mode = null;
    areaState.index = -1;
    areaState.origin = null;
    if (layer.hasPointerCapture?.(event.pointerId)) layer.releasePointerCapture(event.pointerId);
    renderAreaBoxes();
  };
  layer.addEventListener("pointerup", finish);
  layer.addEventListener("pointercancel", finish);
}

function openAreaEditor() {
  areaState.areas = clone(state.config.background.writing_areas || []);
  areaState.mode = null;
  areaState.index = -1;
  areaState.selected = -1;
  areaState.origin = null;
  $("#area-modal").hidden = false;
  syncAreaStage();
  renderAreaBoxes();
}

// ---- 分节背景 ----

const sectionState = { entries: [], headings: [], level: 1 };

async function openSectionEditor() {
  sectionState.entries = clone(getPath(state.config, "background.section_images") || []).filter(
    (entry) => entry && entry.heading && entry.image,
  );
  sectionState.level = Number(getPath(state.config, "background.section_level") || 1);
  $("#section-level").value = String(sectionState.level);
  $("#section-modal").hidden = false;
  await loadSectionHeadings();
}

function closeSectionEditor() {
  $("#section-modal").hidden = true;
}

async function loadSectionHeadings() {
  const list = $("#section-list");
  list.replaceChildren();
  const loading = document.createElement("p");
  loading.className = "panel-note";
  loading.textContent = "正在读取文档标题…";
  list.append(loading);
  try {
    const response = await fetch("/api/outline", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ markdown: $("#markdown-editor").value, level: sectionState.level }),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "读取标题失败");
    sectionState.headings = result.headings || [];
    renderSectionRows();
  } catch (error) {
    list.replaceChildren();
    const failed = document.createElement("p");
    failed.className = "panel-note";
    failed.textContent = error.message;
    list.append(failed);
  }
}

function renderSectionRows() {
  const list = $("#section-list");
  list.replaceChildren();
  const headings = [...new Set(sectionState.headings)];
  const selected = new Map(sectionState.entries.map((entry) => [entry.heading, entry.image]));
  if (!headings.length) {
    const empty = document.createElement("p");
    empty.className = "panel-note";
    empty.textContent = `文档里还没有${["一", "二", "三"][sectionState.level - 1]}级标题。先写标题，或换一个分节级别。`;
    list.append(empty);
    updateSectionCount();
    return;
  }
  const assets = (state.assets.backgrounds || []).filter((asset) => !asset.value.toLowerCase().endsWith(".pdf"));
  const known = new Set(assets.map((asset) => asset.value));
  headings.forEach((heading) => {
    const row = document.createElement("div");
    row.className = "section-row";
    const title = document.createElement("span");
    title.className = "section-row-title";
    title.textContent = heading;
    const select = document.createElement("select");
    select.className = "select";
    select.setAttribute("aria-label", `${heading} 的背景`);
    const none = document.createElement("option");
    none.value = "";
    none.textContent = "沿用全局背景";
    select.append(none);
    assets.forEach((asset) => {
      const option = document.createElement("option");
      option.value = asset.value;
      option.textContent = asset.label;
      select.append(option);
    });
    const current = selected.get(heading) || "";
    if (current && !known.has(current)) {
      const custom = document.createElement("option");
      custom.value = current;
      custom.textContent = current;
      select.append(custom);
    }
    select.value = current;
    select.addEventListener("change", () => {
      sectionState.entries = sectionState.entries.filter((entry) => entry.heading !== heading);
      if (select.value) sectionState.entries.push({ heading, image: select.value });
      updateSectionCount();
    });
    row.append(title, select);
    list.append(row);
  });
  updateSectionCount();
}

function updateSectionCount() {
  const count = $("#section-count");
  if (count) {
    count.textContent = sectionState.entries.length ? `${sectionState.entries.length} 节已指定` : "尚未指定";
  }
}

function applySectionEditor() {
  setPath(state.config, "background.section_level", Number($("#section-level").value) || 1);
  setPath(
    state.config,
    "background.section_images",
    sectionState.entries.map((entry) => ({ heading: entry.heading, image: entry.image })),
  );
  syncSectionSummary();
  markChanged();
  closeSectionEditor();
  showToast(
    "分节背景已更新",
    sectionState.entries.length
      ? `${sectionState.entries.length} 节会换底图并另起一页，重新预览后生效。`
      : "已全部恢复为全局背景。",
  );
}

function closeAreaEditor() {
  $("#area-modal").hidden = true;
  areaState.mode = null;
  areaState.index = -1;
  areaState.origin = null;
}

function applyAreaSelection() {
  const cleaned = areaState.areas
    .map((area) => {
      const x = round4(clamp01(area.x));
      const y = round4(clamp01(area.y));
      return {
        x,
        y,
        w: round4(Math.min(area.w, 1 - x)),
        h: round4(Math.min(area.h, 1 - y)),
      };
    })
    .filter((area) => area.w >= AREA_MIN && area.h >= AREA_MIN);
  setPath(state.config, "background.writing_areas", cleaned);
  syncAreaSummary();
  markChanged();
  closeAreaEditor();
  showToast(
    "书写区域已更新",
    cleaned.length
      ? `${cleaned.length} 个区域，正文按序号依次填写，写满后换页。`
      : "已恢复为按页边距整页排版。",
  );
}

function applyMacro(id) {
  const slider = $(`#${id}`);
  const percent = Number(slider.value);
  $(`#${id}-value`).textContent = `${percent}%`;
  updateRangePaint(slider);
  const updates = MACRO_FIELDS[id](percent, state.config);
  Object.entries(updates).forEach(([path, value]) => setPath(state.config, path, value));
}

function bindMacro(id) {
  const slider = $(`#${id}`);
  slider.addEventListener("input", () => {
    applyMacro(id);
    markChanged();
  });
  // 松手后刷新高级设置里的对应数值，保持两处显示一致。
  slider.addEventListener("change", () => renderConfigSections());
}

function updateDocumentStats() {
  const value = $("#markdown-editor").value;
  $("#char-count").textContent = value.length.toLocaleString("zh-CN");
  $("#line-count").textContent = (value ? value.split(/\r?\n/).length : 0).toLocaleString("zh-CN");
  $("#save-state").textContent = "文稿已更新";
  $("#save-state").style.color = "var(--ink-faint)";
}

function revealEditor() {
  $("#editor-wrap").hidden = false;
  $("#editor-toggle").setAttribute("aria-expanded", "true");
}

async function loadExample(name, notify = true) {
  if (!name) return;
  try {
    const response = await fetch(`/api/examples/${encodeURIComponent(name)}`);
    if (!response.ok) throw new Error("示例读取失败");
    $("#markdown-editor").value = await response.text();
    revealEditor();
    updateDocumentStats();
    if (notify) showToast("示例已载入", "可以继续编辑，或直接生成预览。");
  } catch (error) {
    showToast("无法载入示例", error.message, true);
  }
}

function applyPreset(name) {
  const values = state.presets[name];
  if (!values) return;
  Object.entries(values).forEach(([path, value]) => setPath(state.config, path, value));
  renderConfigSections();
  $$("[data-preset]").forEach((button) => button.classList.toggle("active", button.dataset.preset === name));
  const names = { formal: "工整", natural: "自然", casual: "随性" };
  showToast("风格已切换", `已应用“${names[name]}”扰动参数，可继续微调。`);
}

function showToast(title, message, error = false) {
  const toast = $("#toast");
  if (state.toastTimer) clearTimeout(state.toastTimer);
  $("#toast-title").textContent = title;
  $("#toast-message").textContent = message;
  $(".toast-mark", toast).textContent = error ? "!" : "✓";
  toast.classList.toggle("error", error);
  toast.hidden = false;
  requestAnimationFrame(() => toast.classList.add("show"));
  state.toastTimer = setTimeout(() => {
    toast.classList.remove("show");
    setTimeout(() => { toast.hidden = true; }, 260);
  }, error ? 5200 : 3600);
}

// ---- 帮助浮层 ----

const HELP_TEXT = {
  faq: [
    ["公式没显示？", "行内公式用 <code>$...$</code> 或 <code>\\(...\\)</code>，行间公式用 <code>$$...$$</code> 独占一行。"],
    ["预览和下载一致吗？", "完全一致：预览图与下载的 PDF 取自同一次渲染的同一份栅格，像素逐点相同，没有压缩或缩放。"],
    ["怎么检查清晰度？", "点工具栏的 1:1 按钮按原始像素显示，再放大到 400% 逐笔检查；右上角会标出本页的实际像素尺寸。"],
    ["清晰度为什么不是我设的值？", "预览按你设定的 DPI 渲染；只有当上一轮实测单页超过 5 秒预算时才会自动降档，实际 DPI 会显示在提示里。"],
    ["参数能保存吗？", "刷新页面会回到默认值，可用顶部“导出配置”保存 JSON，下次“导入配置”恢复。"],
    ["字体没生效？", "把 .ttf/.otf 放进项目的 <code>font/</code> 目录后重启服务，即可在下拉框里选到。"],
    ["文字写到了背景的花纹上？", "点“设定书写区域”，在导入的背景上框出正文可以书写的部分；可以框多个区域，正文会按序号依次填满。"],
  ],
  fonts: [
    ["可选字体", "下拉框列出 <code>font/</code> 目录下的字体；选“自动”则按目录顺序取第一个。"],
    ["纸张背景", "纯色纸适合打印；横线/方格/点阵是程序绘制的版式纸。点“导入背景图片”可直接上传实拍纸纹（PNG/JPEG/WebP/BMP/PDF），文件保存到 <code>background/uploads/</code>。"],
    ["书写区域", "导入背景后点“设定书写区域”，在背景上拖拽框出正文可以书写的部分。多个区域按序号依次填写，全部写满后才换页，适合有表头、装订线或分栏的纸张。"],
    ["飞白与涂写", "四条滑杆分别控制飞白（干笔）、涂写划掉、文字位置偏移和笔画变形，出厂默认即自然手写。"],
  ],
};

function openHelp(kind) {
  const popover = $("#help-popover");
  $("#help-title").textContent = kind === "faq" ? "常见问题" : "字体与纸张背景";
  $("#help-body").innerHTML = HELP_TEXT[kind]
    .map(([title, body]) => `<h3>${title}</h3><p>${body}</p>`)
    .join("");
  popover.hidden = false;
}

function closeHelp() {
  $("#help-popover").hidden = true;
}

// ---- 渲染 ----

function setRendering(rendering) {
  state.rendering = rendering;
  $("#render-overlay").hidden = !rendering;
  ["#render-main", "#render-empty"].forEach((selector) => {
    const button = $(selector);
    button.disabled = rendering;
    button.classList.toggle("loading", rendering);
  });
  const dot = $(".status-dot");
  dot.classList.toggle("busy", rendering);
  dot.classList.remove("error");
  $("#service-status").textContent = rendering
    ? "正在生成预览（按单页 5 秒预算自适应 DPI）…"
    : "本地渲染器已连接";
  if (rendering) {
    state.renderStartedAt = Date.now();
    $("#render-title").textContent = "正在生成预览";
    $("#render-time").textContent = "00:00";
    state.renderTimer = window.setInterval(() => {
      const seconds = Math.floor((Date.now() - state.renderStartedAt) / 1000);
      $("#render-time").textContent = `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
      const steps = ["解析 Markdown 与公式…", "计算版面与分页…", "逐行书写文字…", "施加墨迹扰动…", "合成预览图…"];
      $("#render-step").textContent = steps[Math.min(steps.length - 1, Math.floor(seconds / 3))];
    }, 500);
  } else if (state.renderTimer) {
    clearInterval(state.renderTimer);
    state.renderTimer = null;
  }
}

async function renderDocument() {
  if (state.rendering) return false;
  const markdown = $("#markdown-editor").value;
  if (!markdown.trim()) {
    revealEditor();
    $("#markdown-editor").focus();
    showToast("文稿为空", "请先上传、输入或载入示例 Markdown 内容。", true);
    return false;
  }
  setRendering(true);
  try {
    const response = await fetch("/api/render", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ markdown, config: state.config, mode: "preview" }),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "生成失败");
    state.pages = result.pages;
    state.page = 0;
    state.autoFit = true;
    const download = $("#download-pdf");
    download.href = result.pdf;
    download.setAttribute("download", "markdown-to-handwrite.pdf");
    download.classList.remove("disabled");
    download.setAttribute("aria-disabled", "false");
    $("#empty-state").hidden = true;
    $("#preview-image").hidden = false;
    showCurrentPage(result);
    $("#save-state").textContent = "预览已是最新";
    $("#save-state").style.color = "var(--green)";
    const seconds = Math.max(1, Math.round((Date.now() - state.renderStartedAt) / 1000));
    const pixels = `${result.pixelWidth} × ${result.pixelHeight}`;
    $("#page-pixels").textContent = `${pixels} 像素`;
    showToast(
      "预览已生成",
      `${result.pageCount} 页 · ${result.dpi} DPI · ${pixels} 像素 · 单页 ${result.secondsPerPage} 秒 · 用时 ${seconds} 秒`,
    );
    $("#dpi-hint").textContent = `预览实际 ${result.dpi} DPI · 单页 ${result.secondsPerPage} 秒（预算 5 秒）`;
    $("#stage-hint").textContent =
      `预览 ${pixels} 像素 · ${result.dpi} DPI · 与“下载文件”内容完全一致`;
    return true;
  } catch (error) {
    $(".status-dot").classList.add("error");
    $("#service-status").textContent = "生成遇到问题";
    showToast("生成失败", error.message, true);
    return false;
  } finally {
    setRendering(false);
  }
}

function showCurrentPage(result) {
  if (!state.pages.length) return;
  const image = $("#preview-image");
  image.src = state.pages[state.page];
  image.alt = `手写报告第 ${state.page + 1} 页`;
  $("#current-page").textContent = state.page + 1;
  $("#page-count").textContent = state.pages.length;
  $("#prev-page").disabled = state.page <= 0;
  $("#next-page").disabled = state.page >= state.pages.length - 1;
}

function turnPage(delta) {
  const next = state.page + delta;
  if (next < 0 || next >= state.pages.length) return;
  state.page = next;
  showCurrentPage();
}

// ---- 缩放 ----

function naturalWidth() {
  const image = $("#preview-image");
  return image.naturalWidth || state.naturalWidth || 0;
}

function setZoom(value) {
  const zoom = Math.max(ZOOM_MIN, Math.min(ZOOM_MAX, value));
  state.zoom = zoom;
  const natural = naturalWidth();
  const shell = $("#paper-shell");
  shell.style.width = `${Math.round((natural || EMPTY_PAPER_WIDTH) * zoom)}px`;
  $("#zoom-label").textContent = `${Math.round(zoom * 100)}%`;
}

function fitPreview() {
  const viewport = $("#canvas-viewport");
  const natural = naturalWidth();
  if (!natural) {
    setZoom(1);
    return;
  }
  const available = viewport.clientWidth - 48;
  setZoom(Math.min(1, Math.max(ZOOM_MIN, available / natural)));
}

function zoomByFactor(factor) {
  setZoom(state.zoom * factor);
}

// ---- 导入导出与主题 ----

function downloadJson(filename, data) {
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  URL.revokeObjectURL(url);
}

function applyTheme(theme) {
  document.body.dataset.theme = theme;
  localStorage.setItem("markdown-to-handwrite-theme", theme);
}

// ---- 事件绑定 ----

function bindEvents() {
  $("#editor-toggle").addEventListener("click", () => {
    const wrap = $("#editor-wrap");
    if (wrap.hidden) {
      revealEditor();
      $("#markdown-editor").focus();
    } else {
      wrap.hidden = true;
      $("#editor-toggle").setAttribute("aria-expanded", "false");
    }
  });

  $("#example-select").addEventListener("change", (event) => loadExample(event.target.value));

  $("#markdown-import").addEventListener("change", async (event) => {
    const file = event.target.files?.[0];
    if (!file) return;
    $("#markdown-editor").value = await file.text();
    revealEditor();
    updateDocumentStats();
    event.target.value = "";
    showToast("文稿已导入", file.name);
  });

  $("#markdown-editor").addEventListener("input", updateDocumentStats);

  $("#help-open").addEventListener("click", () => openHelp("faq"));
  $("#help-fonts").addEventListener("click", () => openHelp("fonts"));
  $("#help-close").addEventListener("click", closeHelp);
  document.addEventListener("click", (event) => {
    const popover = $("#help-popover");
    if (popover.hidden) return;
    if (!popover.contains(event.target) && !event.target.closest("#help-open, #help-fonts")) closeHelp();
  });

  $("#config-import").addEventListener("change", async (event) => {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file) return;
    try {
      const imported = JSON.parse(await file.text());
      state.config = mergeDeep(clone(state.defaults), imported);
      syncPrimaryControls();
      renderConfigSections();
      markChanged();
      showToast("配置已导入", file.name);
    } catch (error) {
      showToast("配置无法导入", "请确认文件是有效的 JSON 配置。", true);
    }
  });
  $("#config-export").addEventListener("click", () => downloadJson("markdown-to-handwrite-config.json", state.config));

  $("#theme-toggle").addEventListener("click", () => {
    applyTheme(document.body.dataset.theme === "dark" ? "light" : "dark");
  });

  $("#reset-config").addEventListener("click", () => {
    state.config = clone(state.defaults);
    Object.entries(MACRO_DEFAULTS).forEach(([id, value]) => {
      $(`#${id}`).value = value;
      applyMacro(id);
    });
    syncPrimaryControls();
    renderConfigSections();
    $$("[data-preset]").forEach((button) => button.classList.toggle("active", button.dataset.preset === "natural"));
    showToast("已恢复默认参数", "所有分组均已重置。");
  });

  $("#config-search").addEventListener("input", applyConfigSearch);

  $$("[data-preset]").forEach((button) => {
    button.addEventListener("click", () => applyPreset(button.dataset.preset));
  });

  $("#font-select").addEventListener("change", (event) => {
    const value = event.target.value;
    if (value.startsWith("style:")) {
      const path = value.slice("style:".length);
      const profile = (state.assets.styleProfiles || []).find((item) => item.value === path);
      if (profile) {
        Object.entries(profile.style || {}).forEach(([key, styleValue]) => setPath(state.config, key, styleValue));
        if (profile.glyphLibrary) setPath(state.config, "handwriting.glyph_library", profile.glyphLibrary);
        state.activeStyleProfile = path;
        markChanged();
      }
      return;
    }
    state.activeStyleProfile = null;
    if (value.startsWith("glyph:")) {
      setPath(state.config, "handwriting.glyph_library", value.slice("glyph:".length));
      markChanged();
      return;
    }
    setPath(state.config, "handwriting.glyph_library", null);
    setPath(
      state.config,
      "handwriting.font_path",
      value.startsWith("font:") ? value.slice("font:".length) : null,
    );
    markChanged();
  });

  $("#paper-style-select").addEventListener("change", (event) => {
    const style = event.target.value;
    setPath(state.config, "background.style", style);
    if (style !== "image") setPath(state.config, "background.image", null);
    syncPaperControls();
    markChanged();
  });

  $("#paper-image-select").addEventListener("change", (event) => {
    const next = event.target.value || null;
    const changed = next !== state.config.background.image;
    setPath(state.config, "background.image", next);
    const dropped = changed ? clearLineCalibration() : false;
    if (dropped) {
      showToast("已换背景", "之前的横线标定是按旧图算的，已清除；请对新背景重新标定横线。");
    }
    markChanged();
  });

  $("#custom-paper").addEventListener("click", () => {
    setPath(state.config, "background.style", "image");
    syncPaperControls();
    markChanged();
    $("#paper-image-select").focus();
  });

  $("#paper-upload").addEventListener("change", async (event) => {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (file) await importBackground(file);
  });

  $("#style-upload").addEventListener("change", async (event) => {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (file) await analyzeStyleSample(file);
  });
  $("#download-template").addEventListener("click", downloadTemplate);
  $("#detect-lines").addEventListener("click", detectRuledLines);
  $("#open-line-editor").addEventListener("click", openLineEditor);
  $("#open-rectify").addEventListener("click", openRectify);
  $("#rectify-close").addEventListener("click", closeRectify);
  $("#rectify-apply").addEventListener("click", applyRectify);
  $("#rectify-stage").addEventListener("click", handleRectifyClick);
  $("#rectify-reset").addEventListener("click", () => {
    rectifyState.picks = [];
    $("#rectify-summary").textContent = "";
    drawRectifyOverlay();
  });
  $("#rectify-modal").addEventListener("click", (event) => {
    if (event.target === event.currentTarget) closeRectify();
  });
  $("#line-close").addEventListener("click", closeLineEditor);
  $("#line-apply").addEventListener("click", applyLineCalibration);
  $("#line-stage").addEventListener("click", handleLineClick);
  $("#line-reset").addEventListener("click", () => {
    lineState.picks = [];
    lineState.preview = [];
    updateLinePreview();
  });
  $("#line-modal").addEventListener("click", (event) => {
    if (event.target === event.currentTarget) closeLineEditor();
  });
  $("#glyph-upload").addEventListener("change", async (event) => {
    const files = Array.from(event.target.files || []);
    event.target.value = "";
    if (files.length) await buildGlyphLibrary(files);
  });
  $("#style-close").addEventListener("click", closeStyleModal);
  $("#style-cancel").addEventListener("click", closeStyleModal);
  $("#style-apply").addEventListener("click", applyStyleResult);
  $("#style-save").addEventListener("click", saveStyleProfile);
  $("#style-modal").addEventListener("click", (event) => {
    if (event.target === event.currentTarget) closeStyleModal();
  });

  $("#open-area-editor").addEventListener("click", () => openAreaEditor());
  $("#area-close").addEventListener("click", closeAreaEditor);
  $("#area-cancel").addEventListener("click", closeAreaEditor);
  $("#area-apply").addEventListener("click", applyAreaSelection);
  $("#area-modal").addEventListener("click", (event) => {
    if (event.target === event.currentTarget) closeAreaEditor();
  });
  $("#area-clear").addEventListener("click", () => {
    areaState.areas = [];
    areaState.selected = -1;
    renderAreaBoxes();
  });
  $("#area-fill").addEventListener("click", () => {
    areaState.areas = [{ x: 0, y: 0, w: 1, h: 1 }];
    areaState.selected = 0;
    renderAreaBoxes();
  });
  $("#area-margin").addEventListener("click", () => {
    areaState.areas = marginArea();
    areaState.selected = 0;
    renderAreaBoxes();
  });
  bindAreaStage();

  $("#section-close").addEventListener("click", closeSectionEditor);
  $("#section-cancel").addEventListener("click", closeSectionEditor);
  $("#section-apply").addEventListener("click", applySectionEditor);
  $("#section-level").addEventListener("change", () => {
    sectionState.level = Number($("#section-level").value) || 1;
    loadSectionHeadings();
  });
  $("#section-clear").addEventListener("click", () => {
    sectionState.entries = [];
    renderSectionRows();
  });
  $("#section-modal").addEventListener("click", (event) => {
    if (event.target === event.currentTarget) closeSectionEditor();
  });

  const dpiRange = $("#dpi-range");
  dpiRange.addEventListener("input", () => {
    const dpi = Number(dpiRange.value);
    setPath(state.config, "page.dpi", dpi);
    $("#dpi-value").textContent = `${dpi} DPI`;
    updateRangePaint(dpiRange);
    markChanged();
  });

  Object.keys(MACRO_FIELDS).forEach(bindMacro);

  $("#render-main").addEventListener("click", () => renderDocument());
  $("#render-empty").addEventListener("click", () => renderDocument());

  $("#download-pdf").addEventListener("click", (event) => {
    if (event.currentTarget.classList.contains("disabled")) event.preventDefault();
  });

  $("#prev-page").addEventListener("click", () => turnPage(-1));
  $("#next-page").addEventListener("click", () => turnPage(1));
  $("#zoom-in").addEventListener("click", () => zoomByFactor(1.25));
  $("#zoom-out").addEventListener("click", () => zoomByFactor(1 / 1.25));
  $("#zoom-fit").addEventListener("click", fitPreview);
  $("#zoom-actual").addEventListener("click", () => setZoom(1));

  $("#preview-image").addEventListener("load", () => {
    state.naturalWidth = $("#preview-image").naturalWidth;
    if (state.autoFit) {
      state.autoFit = false;
      setZoom(DEFAULT_ZOOM);
    }
  });

  document.addEventListener("keydown", (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
      event.preventDefault();
      renderDocument();
    }
    const areaModal = $("#area-modal");
    const areaOpen = areaModal && !areaModal.hidden;
    const styleModal = $("#style-modal");
    const styleOpen = styleModal && !styleModal.hidden;
    const lineModal = $("#line-modal");
    const lineOpen = lineModal && !lineModal.hidden;
    const rectifyModal = $("#rectify-modal");
    const rectifyOpen = rectifyModal && !rectifyModal.hidden;
    const sectionModal = $("#section-modal");
    const sectionOpen = sectionModal && !sectionModal.hidden;
    if (event.key === "Escape") {
      if (sectionOpen) {
        closeSectionEditor();
        return;
      }
      if (areaOpen) {
        closeAreaEditor();
        return;
      }
      if (styleOpen) {
        closeStyleModal();
        return;
      }
      if (lineOpen) {
        closeLineEditor();
        return;
      }
      if (rectifyOpen) {
        closeRectify();
        return;
      }
      closeHelp();
      return;
    }
    if (areaOpen && (event.key === "Delete" || event.key === "Backspace") && areaState.selected >= 0) {
      areaState.areas.splice(areaState.selected, 1);
      areaState.selected = -1;
      renderAreaBoxes();
      event.preventDefault();
    }
  });

  window.addEventListener("resize", () => {
    if (!state.pages.length) fitPreview();
  });
}

// ---- 启动 ----

async function initialize() {
  applyTheme(localStorage.getItem("markdown-to-handwrite-theme") || "light");
  try {
    const response = await fetch("/api/bootstrap");
    if (!response.ok) throw new Error("无法读取配置模型");
    const bootstrap = await response.json();
    state.defaults = bootstrap.config;
    state.config = clone(bootstrap.config);
    state.sections = bootstrap.sections;
    state.presets = bootstrap.presets;
    state.assets = bootstrap.assets;
    state.examples = bootstrap.examples || [];
    state.template = bootstrap.template || null;
  } catch (error) {
    $(".status-dot").classList.add("error");
    $("#service-status").textContent = "无法连接渲染器";
    showToast("WebUI 初始化失败", error.message, true);
    return;
  }

  const exampleSelect = $("#example-select");
  const placeholder = document.createElement("option");
  placeholder.value = "";
  placeholder.textContent = "🕐 示例文稿";
  exampleSelect.append(placeholder);
  state.examples.forEach((example) => {
    const option = document.createElement("option");
    option.value = example.value;
    option.textContent = example.label;
    exampleSelect.append(option);
  });

  syncPrimaryControls();
  Object.keys(MACRO_FIELDS).forEach(applyMacro);
  renderConfigSections();
  bindEvents();

  const preferred = state.examples.find((example) => example.value === "sample.md") || state.examples[0];
  if (preferred) {
    exampleSelect.value = preferred.value;
    await loadExample(preferred.value, false);
  }
  requestAnimationFrame(fitPreview);
}

initialize();
