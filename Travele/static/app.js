// ==========================================================================
// Travele 前端全部交互（唯一一份脚本，四个视图共用）
//
// 读的顺序：
//   一、公共小工具        取元素/转义/文案表/图标/纸张色调
//   二、视图切换          顶部索引标签在"首页"/"目的地"/"路线"/"手记"之间切
//   三、首页              封面照贴 + 最近的手记
//   四、目的地            洗牌抽目的地（不调模型、不查接口）+ 照片墙
//   五、需求表单          表单 -> 请求体
//   六、提交与进度        提交拿 tripId，跟 SSE 看进度
//   七、画行程            把后端返回的行程画成可点的方案树
//   八、本地切换          换地点/换交通，零请求
//   九、确认行程          只把改过的节点当决策表发回去
//   十、启动              绑定按钮 + 按地址栏的 # 决定先看哪个视图
//   十一、手记            拉确认过的行程列表，点开看完整那一版（只读）
// ==========================================================================

// ---------------- 一、公共小工具 ----------------
const el = (id) => document.getElementById(id);

/** 往 HTML 里塞文本前先转义，免得内容里的字符把页面搞坏 */
function esc(value){
  return String(value === null || value === undefined ? "" : value).replace(/[&<>"']/g,
    (c) => ({ "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;" }[c]));
}

/** 进度事件名 -> 中文标签（后端发的是英文名）。
 *  这些字是给"出门旅游的人"看的，不是给开发者看的：说人话，
 *  别出现"召回/降级/Agent/单次生成"这类词。 */
const PROGRESS_LABEL = {
  searching:"查资料", drafting:"排行程", validating:"做检查",
  revising:"调整", done:"完成", failed:"失败",
};
/** 交通方式 -> 中文标签 */
const TRANSPORT_LABEL = {
  taxi:"打车", bike:"骑行", walk:"步行", bus:"公交", subway:"地铁",
  drive:"自驾", train:"火车", boat:"乘船", plane:"飞机",
};
/** 时段 -> 中文标签（后端给了 slotLabel 就用后端的） */
const SLOT_LABEL = { morning:"上午", afternoon:"下午", evening:"晚上" };

function showError(boxId, text){
  const box = el(boxId);
  box.textContent = text;
  box.hidden = false;
}
function hideError(boxId){ el(boxId).hidden = true; }

/** Lucide 把 <i data-lucide> 换成 svg；每次重画之后要再叫一次 */
function refreshIcons(){
  if (window.lucide && typeof window.lucide.createIcons === "function") window.lucide.createIcons();
}

// 纸张色调：没有真照片，用"褪色相纸"的几组渐变当底，同一个地名每次颜色一样。
// 只从手账的四族色里取：牛皮纸黄 / 墨绿 / 砖红 / 褪色蓝。
const TINTS = [
  "linear-gradient(150deg,#dcc9a0,#b79f6f)",
  "linear-gradient(150deg,#c3cdb6,#8a9c82)",
  "linear-gradient(150deg,#dcae96,#c07a5c)",
  "linear-gradient(150deg,#bccbdb,#8aa2ba)",
  "linear-gradient(150deg,#e2d3ae,#c0aa7c)",
  "linear-gradient(150deg,#a9bda9,#7c947e)",
];
function tintFor(text){
  let hash = 0;
  for (const ch of text) hash = (hash * 31 + ch.codePointAt(0)) % 9973;
  return TINTS[hash % TINTS.length];
}

// ---------------- 二、视图切换 ----------------
const VIEW_NAMES = ["home", "discover", "plan", "history"];
const tabs = Array.from(document.querySelectorAll(".tab"));

function viewNameFromHash(){
  const name = location.hash.replace(/^#\/?/, "");
  return VIEW_NAMES.includes(name) ? name : "home";
}

/** 切视图。同时是几处跳转（导航、封面按钮、卡片页"就它了"）的唯一出口 */
function showView(name, updateHash){
  for (const view of VIEW_NAMES) el("view-" + view).hidden = (view !== name);
  for (const tab of tabs) tab.classList.toggle("on", tab.dataset.view === name);
  if (updateHash !== false) location.hash = "#/" + name;
  if (name === "history") loadHistory();          // 每次进这个标签都重新读一遍
  if (name === "home") loadHome();
  observeReveals(el("view-" + name));
  window.scrollTo(0, 0);
}

for (const tab of tabs){
  tab.addEventListener("click", () => showView(tab.dataset.view));
}
window.addEventListener("hashchange", () => showView(viewNameFromHash(), false));

/** 滚动出现：进视口就贴上（动效在 app.css 的 .reveal） */
let revealObserver = null;
function observeReveals(root){
  const nodes = Array.from((root || document).querySelectorAll(".reveal"));
  if (!revealObserver){ nodes.forEach((node) => node.classList.add("in")); return; }
  for (const node of nodes){
    if (!node.classList.contains("in")) revealObserver.observe(node);
  }
}
function setupReveal(){
  if (!("IntersectionObserver" in window)) return;     // 老浏览器：不搞动效，全部直接显示
  revealObserver = new IntersectionObserver((entries) => {
    for (const entry of entries){
      if (!entry.isIntersecting) continue;
      entry.target.classList.add("in");
      revealObserver.unobserve(entry.target);
    }
  }, { threshold: 0.06, rootMargin: "0px 0px -6% 0px" });
}

// ---------------- 三、首页（手账封面） ----------------
/** 手记卡片上那行小字：地点 ｜ 日期 ｜ 确认时间 */
function planMetaLine(item){
  const when = [item.startDate || "", item.endDate ? "至 " + item.endDate : ""].filter(Boolean).join(" ");
  const savedAt = (item.savedAt || "").replace("T", " ").slice(0, 16);
  const place = [item.province, item.city].filter(Boolean).join(" ");
  return [place, when, savedAt ? "确认于 " + savedAt : ""].filter(Boolean).join(" ｜ ");
}

/** 封面上那几张照贴：每次进来随机 6 张，都出自真实目录 */
function renderCoverWall(){
  if (!spots.length) return;
  const picks = shuffledIndexes(spots.length).slice(0, 6).map((i) => spots[i]);
  el("coverWall").innerHTML = picks.map(spotCard).join("");
  observeReveals(el("coverWall"));
}

/** 首页：封面照贴 + 最近的手记 3 条 */
async function loadHome(){
  renderCoverWall();
  try {
    const response = await fetch("/api/plans");
    if (!response.ok) throw new Error("HTTP " + response.status);
    const items = await response.json();
    el("homeRecent").innerHTML = items.length
      ? items.slice(0, 3).map((item) =>
          '<button type="button" class="plan-card" data-plan="' + esc(item.planId) + '">'
          + '<span class="plan-title">' + esc(item.title || "未命名行程") + "</span>"
          + '<span class="dim small">' + esc(planMetaLine(item)) + "</span></button>").join("")
        + '<p class="dim small">完整列表在「手记」那页；没确认过的行程不会贴上来。</p>'
      : '<p class="dim small">还没有手记。去「路线」排一份、点确认，它就会贴到这里。</p>';
  } catch (error) {
    el("homeRecent").innerHTML = '<p class="dim small">读不到手记（' + esc(error.message) + "）。</p>";
  }
}

// ---------------- 四、目的地（随机发现 + 照片墙） ----------------
// 只显示客观信息（省市/景点/描述），不做"好不好玩"的判断
let spots = [];        // 全部目的地
let bag = [];          // 本轮还没抽过的下标（抽完一轮才重洗，保证不重复）
let seen = 0;          // 本轮已看几张
let currentSpot = null; // 当前显示的那一条

/** 洗牌（Fisher-Yates），返回打乱后的下标 */
function shuffledIndexes(total){
  const indexes = Array.from({ length: total }, (_, i) => i);
  for (let i = indexes.length - 1; i > 0; i--){
    const j = Math.floor(Math.random() * (i + 1));
    [indexes[i], indexes[j]] = [indexes[j], indexes[i]];
  }
  return indexes;
}

/** 抽下一张 */
function drawSpot(){
  if (spots.length === 0) return;
  if (bag.length === 0){                 // 本轮抽完 -> 重洗
    bag = shuffledIndexes(spots.length);
    seen = 0;
  }
  let index = bag.shift();
  // 刚重洗时第一张可能又是刚才那张，和袋子里最后一张换个位置
  if (currentSpot && spots[index].spot === currentSpot.spot && bag.length > 0){
    bag.push(index);
    index = bag.shift();
  }
  seen++;
  currentSpot = spots[index];
  renderSpot(currentSpot);
}

function renderSpot(spot){
  // 随机卡用大图：把 <img> 插进 .photo 里（**不能整块替换 innerHTML** ——
  // 省市标签和地点名就在这个容器里，替换掉它们就没了）。
  const visual = el("visual");
  visual.style.setProperty("--tint", tintFor(spot.province + spot.city + spot.spot));
  for (const old of Array.from(visual.querySelectorAll(".photo-img"))) old.remove();
  if (spot.photo){
    visual.insertAdjacentHTML("afterbegin",
      '<img class="photo-img photo-img-hero" src="/photos/' + esc(encodeURIComponent(spot.photo))
      + '" alt="' + esc(spot.spot) + '" decoding="async" onerror="this.remove()">');
  }
  el("place").textContent = spot.province + " · " + spot.city;
  el("spotName").textContent = spot.spot;
  el("spotDesc").textContent = spot.description;
  el("counter").textContent = "本轮第 " + seen + " 个，共 " + spots.length + " 个";
}

/** 照片墙/随机卡上的那张"照片"。
 *  有真图就用真图（Travele/P 下按地点名命名的，后端挂成 /photos/<文件名>），
 *  没有图就退回手绘底色（--tint），不会出现破图。 */
function photoHtml(spot, options){
  const settings = options || {};
  const tint = tintFor(spot.province + spot.city + spot.spot);
  if (!spot.photo){
    return '<div class="photo" style="--tint:' + esc(tint) + '"></div>';
  }
  const source = "/photos/" + encodeURIComponent(spot.photo);
  return '<div class="photo" style="--tint:' + esc(tint) + '">'
       + '<img class="photo-img' + (settings.hero ? " photo-img-hero" : "") + '" src="' + esc(source)
       + '" alt="' + esc(spot.spot) + '" loading="' + (settings.hero ? "eager" : "lazy")
       + '" decoding="async" onerror="this.remove()"></div>';
}

/** 照片墙上的一张：整张可点，点了就把省市填进路线那页 */
function spotCard(spot){
  return '<article class="polaroid reveal" data-wall data-province="' + esc(spot.province)
       + '" data-city="' + esc(spot.city) + '" data-spot="' + esc(spot.spot)
       + '" title="点一下：把省市填进路线那页">'
       + photoHtml(spot)
       + '<div class="photo-name">' + esc(spot.spot) + "</div>"
       + '<span class="wall-meta">' + esc(spot.province + " · " + spot.city) + "</span></article>";
}

async function loadSpots(){
  try {
    const response = await fetch("data/spots.json?v=3");     // 版本号跟 index.html 里的一致，防缓存
    if (!response.ok) throw new Error("HTTP " + response.status);
    spots = await response.json();
    if (!Array.isArray(spots) || spots.length === 0) throw new Error("目录是空的");
    bag = shuffledIndexes(spots.length);
    drawSpot();
    el("spotWall").innerHTML = spots.map(spotCard).join("");
    observeReveals(el("spotWall"));
    renderCoverWall();              // 封面照贴要用它
  } catch (error) {
    el("place").textContent = "";
    el("spotName").textContent = "读不到目录";
    showError("spotsError", "读不到 data/spots.json（" + error.message + "）。"
      + "这个页面要通过后端打开：uvicorn 起来后访问 http://127.0.0.1:8099/ ；"
      + "直接双击本地文件打开的话，浏览器不允许读本地 JSON。");
  }
}

el("nextBtn").addEventListener("click", drawSpot);

/** 选中一张（随机卡或照片墙）-> 把省市填进表单、规划方式记成 discover，再切到路线视图 */
function pickSpot(province, city){
  el("province").value = province;
  el("city").value = city;
  el("mode").value = "discover";
  updateSummary();
  showView("plan");
}
el("pickBtn").addEventListener("click", () => {
  if (currentSpot) pickSpot(currentSpot.province, currentSpot.city);
});
document.addEventListener("click", (event) => {
  const card = event.target.closest("[data-wall]");
  if (card) pickSpot(card.dataset.province, card.dataset.city);
});

el("goPlanBtn").addEventListener("click", () => showView("plan"));
el("coverDiscover").addEventListener("click", () => showView("discover"));
el("coverPlan").addEventListener("click", () => showView("plan"));

// ---------------- 五、需求表单 ----------------
const THEMES = ["自然", "人文", "美食", "亲子", "摄影", "夜生活", "户外", "购物"];
const AVOIDS = ["购物", "爬山", "人多", "早起", "长距离步行"];

/** 预置标签做成复选框：点一下就行，不用手打，也不用解析逗号 */
function buildChips(boxId, names){
  el(boxId).innerHTML = names.map((name, i) => {
    const id = boxId + "-" + i;
    return '<label for="' + id + '"><input type="checkbox" id="' + id + '" value="' + esc(name) + '">'
         + esc(name) + "</label>";
  }).join("");
}

function checkedValues(boxId){
  return Array.from(el(boxId).querySelectorAll("input:checked")).map((input) => input.value);
}

/** 表单 -> 请求体（字段名用 camelCase，和后端契约一致） */
function buildRequest(){
  const budgetTier = el("budgetTier").value;
  return {
    mode: el("mode").value,
    schedule: { tripStart: el("tripStart").value, tripEnd: el("tripEnd").value },
    user: {
      adults: Number(el("adults").value),
      children: Number(el("children").value),
      departureCity: el("departureCity").value.trim(),
      budgetTier: budgetTier || null,
    },
    destination: {
      province: el("province").value.trim(),
      city: el("city").value.trim(),
      centerPlace: el("centerPlace").value.trim() || null,
      mustVisit: el("mustVisit").value.split(/[,，、]/).map((s) => s.trim()).filter(Boolean),
    },
    isSelfDriving: el("isSelfDriving").value === "true",
    prefs: { themes: checkedValues("themeChips"), avoid: checkedValues("avoidChips") },
    remarks: el("remarks").value.trim(),
  };
}

// ---- 快捷需求模板 ----
/** 常见组合，点一下把表单填好，再按需要改 */
const PRESETS = [
  { name: "周末亲子 2 天", days: 2, adults: 2, children: 1, isSelfDriving: false,
    themes: ["亲子"], avoid: ["爬山", "长距离步行"] },
  { name: "美食慢游 3 天", days: 3, adults: 2, children: 0, isSelfDriving: false,
    themes: ["美食", "人文"], avoid: ["早起"] },
  { name: "自驾看风景 5 天", days: 5, adults: 2, children: 0, isSelfDriving: true,
    themes: ["自然", "摄影", "户外"], avoid: ["购物"] },
];
const BUDGET_LABEL = { low: "经济", mid: "中等", high: "舒适" };

function renderPresets(){
  el("presetChips").innerHTML = PRESETS.map((preset, i) =>
    '<button type="button" class="chip" data-preset="' + i + '">'
    + esc(preset.name) + "</button>").join("");
}

/** 按名字勾选（没列出来的自动取消勾选） */
function setChips(boxId, names){
  for (const input of el(boxId).querySelectorAll("input")){
    input.checked = names.includes(input.value);
  }
}

/** 套用快捷需求：日期按"一周后出发 + 天数"算，人数/自驾/偏好一并填好 */
function applyPreset(preset){
  const start = new Date();
  start.setDate(start.getDate() + 7);
  const end = new Date(start);
  end.setDate(end.getDate() + preset.days - 1);
  el("tripStart").value = start.toISOString().slice(0, 10);
  el("tripEnd").value = end.toISOString().slice(0, 10);
  el("adults").value = preset.adults;
  el("children").value = preset.children;
  el("isSelfDriving").value = preset.isSelfDriving ? "true" : "false";
  setChips("themeChips", preset.themes);
  setChips("avoidChips", preset.avoid);
  updateSummary();
  el("remarks").focus();
}

/** 边填边算的需求概要：天数 / 人数 / 目的地 / 交通 / 必去 / 主题 / 预算 */
function updateSummary(){
  const start = el("tripStart").value;
  const end = el("tripEnd").value;
  let days = null;
  if (start && end) days = Math.floor((new Date(end) - new Date(start)) / 86400000) + 1;

  const adults = Number(el("adults").value) || 0;
  const children = Number(el("children").value) || 0;
  const province = el("province").value.trim();
  const city = el("city").value.trim();
  const mustVisit = el("mustVisit").value.split(/[,，、]/).map((s) => s.trim()).filter(Boolean);
  const themes = checkedValues("themeChips");
  const budgetTier = el("budgetTier").value;

  const items = [];
  if (days === null) items.push("日期待定");
  else if (days <= 0) items.push("返回日期早于出发日期");
  else items.push(days + " 天");
  items.push(adults + children > 0
    ? (adults + children) + " 人（" + adults + " 大 " + children + " 小）"
    : "人数待定");
  items.push(province || city ? (province + " " + city).trim() : "目的地待定");
  items.push(el("isSelfDriving").value === "true" ? "自驾" : "不自驾");
  if (mustVisit.length) items.push("必去 " + mustVisit.length + " 个");
  if (themes.length) items.push("主题 " + themes.join("/"));
  if (budgetTier) items.push(BUDGET_LABEL[budgetTier] || budgetTier);

  el("summaryStrip").innerHTML = items
    .map((text) => '<span class="kv-item">' + esc(text) + "</span>")
    .join("");
}

// ---------------- 六、提交与进度 ----------------
/** 后端的统一错误体是 {code, message, detail}，这里把它变成能看的错误 */
class ApiError extends Error {
  constructor(body){
    super((body && body.message) || "请求失败");
    this.code = (body && body.code) || "UNKNOWN";
  }
}
function toMessage(error){
  if (error instanceof ApiError) return "[" + error.code + "] " + error.message;
  return error && error.message ? error.message : String(error);
}

/** 读 JSON：后端返回 500 时可能不是 JSON，别让页面卡在解析上 */
async function readJson(response){
  try { return await response.json(); } catch (e) { return {}; }
}

function appendProgress(type, message){
  const line = document.createElement("li");
  line.className = "p-" + (type || "");
  line.innerHTML = '<span class="ptag">' + esc(PROGRESS_LABEL[type] || type || "进度") + "</span>"
                 + "<span>" + esc(message || "") + "</span>";
  el("progressList").appendChild(line);
  el("progressTitle").textContent = type === "done" ? "规划完成" : "正在规划…";
  markStep(type);
}

// ---- 加载态：四步指示（后端发什么事件就点亮哪一步）+ 秒表 ----
// 这几步只是"让等待看得见"，真实进度以后端的 SSE 为准。
const STEP_ORDER = ["searching", "drafting", "validating", "revising"];
let clockTimer = null;

function markStep(type){
  const list = el("progressSteps");
  if (!list || !list.children) return;
  const items = Array.from(list.children);
  if (items.length === 0) return;

  if (type === "done"){
    for (const item of items){ item.classList.remove("on"); item.classList.add("done"); }
    el("loader").classList.add("quiet");
    stopClock();
    return;
  }
  if (type === "failed"){
    const index = Math.max(0, items.findIndex((item) => item.classList.contains("on")));
    items[index].classList.remove("on");
    items[index].classList.add("failed");
    stopClock();
    return;
  }
  const index = STEP_ORDER.indexOf(type);
  if (index < 0) return;
  items.forEach((item, i) => {
    item.classList.toggle("on", i === index);
    item.classList.toggle("done", i < index);
    item.classList.remove("failed");
  });
}

/** 秒表：真模型一次要几十秒，让它一直转，用户心里有数 */
function startClock(){
  const began = Date.now();
  stopClock();
  el("progressClock").textContent = "已用 0 秒";
  clockTimer = setInterval(() => {
    el("progressClock").textContent = "已用 " + Math.round((Date.now() - began) / 1000) + " 秒";
  }, 1000);
}
function stopClock(){
  if (clockTimer){ clearInterval(clockTimer); clockTimer = null; }
}

/** 每开一轮新规划就把加载态复位 */
function resetLoader(){
  const list = el("progressSteps");
  if (list && list.children){
    for (const item of Array.from(list.children)) item.classList.remove("on", "done", "failed");
  }
  el("loader").classList.remove("quiet");
  el("progressClock").textContent = "已用 0 秒";
}

/**
 * 订阅进度事件（SSE）。后端结尾会发一条 done 或 failed，收到就收工。
 * 等多久：最多 210 秒。真模型一次要 20~90 秒，接了工具之后更慢 —— 等太短会把进度截断；
 * 后端自己的总预算是 180 秒（超了会发 failed「创建超时」），所以正常轮不到这个兜底。
 */
function watchProgress(tripId){
  return new Promise((resolve, reject) => {
    const source = new EventSource("/api/trips/" + encodeURIComponent(tripId) + "/events");
    let settled = false;

    function finish(error){
      if (settled) return;      // 只认第一次结果，避免 done 之后关连接又触发 onerror
      settled = true;
      clearTimeout(timer);
      source.close();
      if (error) reject(error); else resolve();
    }
    const timer = setTimeout(() => finish(new Error("等待进度超时（210 秒）")), 210000);

    source.onmessage = (event) => {
      let data;
      try { data = JSON.parse(event.data); } catch (e) { return; }
      appendProgress(data.event, data.message);
      if (data.event === "done") finish();
      else if (data.event === "failed") finish(new Error(data.message || "规划失败"));
    };
    source.onerror = () => finish(new Error("进度连接中断（后端可能没启动）"));
  });
}

el("planForm").addEventListener("submit", async (event) => {
  event.preventDefault();        // 必填项交给浏览器原生校验，这里不再自己查一遍
  hideError("formError");
  const requestBody = buildRequest();

  el("submitBtn").disabled = true;
  el("progressList").innerHTML = "";
  hideError("progressError");
  hideError("confirmError");
  el("tripPanel").hidden = true;
  el("confirmPanel").hidden = true;
  el("progressPanel").hidden = false;
  el("progressTitle").textContent = "正在提交…";
  resetLoader();
  startClock();
  showView("plan", false);

  try {
    // 第一步：提交需求，后端立刻回一个 tripId（202）
    const submitResponse = await fetch("/api/trips", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(requestBody),
    });
    const receipt = await readJson(submitResponse);
    if (!submitResponse.ok) throw new ApiError(receipt);

    // 第二步：跟着 SSE 看进度，直到 done / failed
    await watchProgress(receipt.tripId);

    // 第三步：进度说完了，用 GET 拿真结果（状态以 GET 为准）
    const tripResponse = await fetch("/api/trips/" + encodeURIComponent(receipt.tripId));
    const tripData = await readJson(tripResponse);
    if (!tripResponse.ok) throw new ApiError(tripData);

    trip = tripData;
    choice.clear();              // 新行程来了，之前的本地选择作废
    renderTrip();
    el("tripPanel").hidden = false;
    observeReveals(el("tripPanel"));
    el("tripPanel").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (error) {
    showError("progressError", "没能拿到行程：" + toMessage(error));
    el("progressTitle").textContent = "规划失败";
    markStep("failed");
  } finally {
    stopClock();
    el("submitBtn").disabled = false;
  }
});

// ---------------- 七、画行程 ----------------
// trip = 后端返回的行程，原样放着，前端绝不改它（改了就算不出"用户改过哪些节点"）
let trip = null;
// choice = 用户在本地改过的节点：节点 key -> { poiId, transport }
const choice = new Map();

/** 一个活动节点在本地选择表里的 key：天-时段-序号 */
function nodeKey(dayIndex, slot, seq){
  return "d" + dayIndex + "-" + slot + "-" + seq;
}
/** 节点默认选中的方案（没改过时就按后端给的第一个） */
function defaultOption(item){
  return (item.options && item.options[0]) || null;
}
/** 方案默认选中的交通（后端标了 recommended 的优先） */
function defaultTransport(option){
  if (!option || !option.transportOptions || option.transportOptions.length === 0) return null;
  return option.transportOptions.find((t) => t.recommended) || option.transportOptions[0];
}
/** 这个节点当前选中的方案 */
function chosenOption(item, key){
  const picked = choice.get(key);
  if (picked && picked.poiId){
    const hit = (item.options || []).find((o) => o.poi && o.poi.poiId === picked.poiId);
    if (hit) return hit;
  }
  return defaultOption(item);
}
/** 这个节点当前选中的交通 */
function chosenTransport(option, key){
  const picked = choice.get(key);
  if (picked && picked.transport){
    const hit = (option.transportOptions || []).find((t) => t.method === picked.transport);
    if (hit) return hit;
  }
  return defaultTransport(option);
}

function renderTrip(){
  el("tripTitle").textContent = trip.title || "行程";
  const destination = trip.destination || {};
  el("tripDest").textContent = [destination.province, destination.city].filter(Boolean).join(" ");
  el("tripBadges").innerHTML = renderBadges();
  el("tripIssues").innerHTML = renderIssues();
  el("tripNotes").innerHTML = renderNotes();
  el("tripIntercity").innerHTML = renderIntercity();
  el("tripLodging").innerHTML = renderLodging();
  el("tripDays").innerHTML = (trip.days || []).map(renderDay).join("");
  el("tripRaw").textContent = JSON.stringify(trip, null, 2);
  el("confirmPanel").hidden = true;
}

/**
 * 大交通：去程 / 回程各一条。
 * 时刻和班次是模型给的、没跟车站核对过，所以标一个"未核对"灰字（后端 verified=false）。
 * 机票票价**没有可靠来源**（Aviationstack 只给班次时刻），后端也不会给模型猜的数，
 * 所以这里对飞机那段一律写"票价无可靠来源"，不显示任何金额。
 */
function renderIntercity(){
  const legs = trip.intercity || [];
  if (!legs.length) return "";
  const rows = legs.map((leg) => {
    const direction = leg.direction === "outbound" ? "去程" : "回程";
    const isPlane = leg.method === "plane";
    const when = [leg.date || "", leg.depart && leg.arrive ? leg.depart + "→" + leg.arrive
                  : (leg.depart || leg.arrive || "")].filter(Boolean).join(" ");
    const head = '<span class="ic-dir">' + esc(direction) + "</span>"
      + '<span class="ic-route">' + esc(leg.fromPlace || "?") + " → " + esc(leg.toPlace || "?") + "</span>"
      + '<span class="badge">' + esc(TRANSPORT_LABEL[leg.method] || leg.method) + "</span>"
      + (leg.verified ? "" : '<span class="dim small">未核对</span>');
    const detail = []
      .concat(when ? [when] : [])
      .concat(leg.detail ? [leg.detail] : [])
      .concat(isPlane ? ["票价无可靠来源，未提供"]
                      : (leg.price ? [leg.price + " 元（参考）"] : []));
    return "<li><div class=\"ic-head\">" + head + "</div>"
      + (detail.length ? '<p class="ic-detail">' + detail.map(esc).join("　") + "</p>" : "")
      + "</li>";
  }).join("");
  return '<section class="leg-block"><h3>大交通</h3><ul class="ic-list">' + rows + "</ul></section>";
}

/** 住宿：一家酒店一段（名字、价格、链接都来自查到的真数据）。 */
function renderLodging(){
  const items = trip.lodging || [];
  if (!items.length) return "";
  const cards = items.map((item) => {
    const price = item.pricePerNight ? esc(item.pricePerNight) + " 元/晚" : "价格没查到";
    const stars = item.starRating ? esc(item.starRating) + " 星" : "";
    const nights = [item.checkIn || "", item.checkOut || ""].filter(Boolean).join(" → ");
    return '<li class="stay">'
      + '<div class="stay-head"><b>' + esc(item.name) + "</b>"
      + (stars ? '<span class="badge">' + stars + "</span>" : "")
      + '<span class="dim small">' + price + "</span></div>"
      + (nights ? '<p class="stay-line">' + esc(nights) + "</p>" : "")
      + (item.address ? '<p class="stay-line">' + esc(item.address) + "</p>" : "")
      + (item.reason ? '<p class="stay-line dim">' + esc(item.reason) + "</p>" : "")
      + (item.bookingUrl ? '<p class="stay-line"><a href="' + esc(item.bookingUrl)
          + '" target="_blank" rel="noreferrer">去预订</a></p>' : "")
      + "</li>";
  }).join("");
  return '<section class="leg-block"><h3>住宿</h3><ul class="stay-list">' + cards + "</ul></section>";
}

function renderBadges(){
  const meta = trip.planMeta || {};
  const badges = [];
  badges.push(meta.validated
    ? '<span class="badge ok">校验通过</span>'
    : '<span class="badge bad">校验未通过</span>');
  const budget = trip.budget;
  if (budget && budget.totalEstimate){
    badges.push('<span class="badge">预算约 ' + esc(budget.totalEstimate) + " " + esc(budget.currency || "CNY")
      + (budget.estimate ? "（估算）" : "") + "</span>");
  }
  const docs = meta.sourceDocs || [];
  if (docs.length) badges.push('<span class="badge">引用攻略：' + esc(docs.join("、")) + "</span>");
  return badges.join("");
}

function renderIssues(){
  const issues = (trip.planMeta && trip.planMeta.issues) || [];
  if (issues.length === 0) return "";
  return '<div class="notice warn"><b>校验发现 ' + issues.length + ' 处问题：</b><ul class="plain">'
       + issues.map((text) => "<li>" + esc(text) + "</li>").join("") + "</ul></div>";
}

/** 后端已经自己处理掉的事（模型编的酒店被剔除、要的交通方式没有改用别的…）。
 *  如实写出来，但不跟"校验没通过"混在一起 —— 这些不影响这版行程能不能用。 */
function renderNotes(){
  const notes = (trip.planMeta && trip.planMeta.notes) || [];
  if (notes.length === 0) return "";
  return '<div class="notice note"><b>这几处后端已经替你处理过：</b><ul class="plain">'
       + notes.map((text) => "<li>" + esc(text) + "</li>").join("") + "</ul></div>";
}

function renderDay(day){
  const segments = (day.segments || []).map((segment) => renderSegment(day, segment)).join("");
  return '<div class="day reveal">'
       + '<div class="day-head"><b>第 ' + esc(day.dayIndex) + " 天</b>"
       + '<span class="dim small">' + esc(day.date || "") + "</span>"
       + (day.theme ? '<span class="theme">' + esc(day.theme) + "</span>" : "")
       + "</div>" + (segments || '<div class="dim small">这天还没有安排</div>') + "</div>";
}

function renderSegment(day, segment){
  const items = (segment.items || []).map((item) => renderItem(day, segment, item)).join("");
  const label = segment.slotLabel || SLOT_LABEL[segment.slot] || segment.slot;
  return '<div class="segment"><div class="seg-head">' + esc(label) + "</div>"
       + (items || '<div class="dim small">未安排</div>') + "</div>";
}

function renderItem(day, segment, item){
  const key = nodeKey(day.dayIndex, segment.slot, item.seq);

  const option = chosenOption(item, key);
  if (!option) return '<div class="item dim small">这个节点没有候选地点</div>';

  const transport = chosenTransport(option, key);
  const totalMin = (option.durationMin || 0) + (transport ? (transport.durationMin || 0) : 0);

  const optionsHtml = (item.options || [])
    .map((candidate) => renderOption(candidate, option, item, key)).join("");
  const transportsHtml = (option.transportOptions || []).length
    ? '<div class="tps">' + option.transportOptions
        .map((t) => renderTransport(t, transport, item, key)).join("") + "</div>"
    : '<div class="dim small" style="margin-top:8px">这个方案没有给交通方式</div>';

  const combo = option.combinationPoi
    ? '<div class="combo">顺路可加：' + esc(option.combinationPoi.name) + "</div>" : "";
  const unverified = (item.unverified || []).length
    ? '<div class="unverified">未经证实：' + esc(item.unverified.join("；")) + "</div>" : "";
  const timeRange = item.timeRange
    ? '<div class="item-time">' + esc(item.timeRange.start) + " - " + esc(item.timeRange.end) + "</div>" : "";

  const summary = "活动 " + (option.durationMin || 0) + " 分钟"
    + (transport ? " + 交通 " + (transport.durationMin || 0) + " 分钟（"
        + esc(TRANSPORT_LABEL[transport.method] || transport.method) + "）" : "")
    + " = 合计 " + totalMin + " 分钟";

  return '<div class="item">' + timeRange + optionsHtml + transportsHtml + combo + unverified
       + '<div class="sum">' + summary + "</div></div>";
}

function renderOption(candidate, currentOption, item, key){
  const poi = candidate.poi || {};
  const on = currentOption && poi.poiId === currentOption.poi.poiId;
  const parts = [];
  if (poi.poiType) parts.push(esc(poi.poiType));
  if (poi.rating) parts.push("评分 " + esc(poi.rating));
  parts.push(esc(candidate.durationMin || 0) + " 分钟");
  if (poi.address) parts.push(esc(poi.address));

  // 有照片就带一张小图（只有一部分点有，没有就纯文字）
  const thumb = poi.photo
    ? '<img class="opt-thumb" src="/photos/' + esc(encodeURIComponent(poi.photo)) + '" alt="' + esc(poi.name || "")
      + '" loading="lazy" decoding="async" onerror="this.remove()">'
    : "";

  return '<button type="button" class="opt' + (on ? " on" : "") + '"'
       + (item.locked ? " disabled" : "")
       + ' data-key="' + esc(key) + '" data-pick-poi="' + esc(poi.poiId) + '">'
       + '<span class="opt-row">' + thumb
       + '<span class="opt-body"><span class="opt-top"><span class="opt-name">'
       + esc(poi.name || "未命名地点") + "</span>"
       + (on ? '<span class="badge agent">已选</span>' : "")
       + (item.locked ? '<span class="badge">已锁定</span>' : "")
       + '</span><span class="dim small">' + parts.join(" · ") + "</span></span></span></button>";
}

function renderTransport(transport, currentTransport, item, key){
  const on = currentTransport && transport.method === currentTransport.method;
  const label = TRANSPORT_LABEL[transport.method] || transport.method;
  const price = transport.price !== undefined && transport.price !== null
    ? "，约 " + transport.price + " 元" : "";
  const reason = transport.recommendReason ? "（" + transport.recommendReason + "）" : "";
  return '<button type="button" class="tp' + (on ? " on" : "") + '"'
       + (item.locked ? " disabled" : "")
       + ' data-key="' + esc(key) + '" data-pick-transport="' + esc(transport.method) + '">'
       + esc(label) + " " + esc(transport.durationMin || 0) + " 分钟" + esc(price) + esc(reason) + "</button>";
}

// ---------------- 八、本地切换（换地点 / 换交通）----------------
// 点一下只改本地选择表，然后重画 —— 不发请求（数据后端已经全给了）
el("tripDays").addEventListener("click", (event) => {
  const button = event.target.closest("[data-pick-poi],[data-pick-transport]");
  if (!button || button.disabled) return;

  const key = button.dataset.key;
  const picked = choice.get(key) || {};

  if (button.dataset.pickPoi){
    // 换地点：之前选的交通不一定还适用，清掉，回到新方案自带的默认交通
    if (picked.poiId && picked.poiId !== button.dataset.pickPoi) delete picked.transport;
    picked.poiId = button.dataset.pickPoi;
  }
  if (button.dataset.pickTransport) picked.transport = button.dataset.pickTransport;
  choice.set(key, picked);

  const scrollTop = window.scrollY;   // 重画会回到顶部，手动还原一下
  renderTrip();
  el("tripPanel").hidden = false;
  observeReveals(el("tripPanel"));
  window.scrollTo(0, scrollTop);
});

/** 本地选择 vs 后端给的默认值：只把"改过的节点"整理成决策表 */
function buildDecisions(){
  const decisions = [];
  for (const day of trip.days || []){
    for (const segment of day.segments || []){
      for (const item of segment.items || []){
        const key = nodeKey(day.dayIndex, segment.slot, item.seq);
        const picked = choice.get(key);
        if (!picked) continue;                    // 没动过 -> 不进决策表

        const original = defaultOption(item);
        const originalTransport = defaultTransport(original);
        const poiChanged = picked.poiId && original && picked.poiId !== original.poi.poiId;
        const transportChanged = picked.transport && originalTransport
          && picked.transport !== originalTransport.method;
        if (!poiChanged && !transportChanged) continue;

        const decision = { dayIndex: day.dayIndex, slot: segment.slot, seq: item.seq };
        if (picked.poiId) decision.chosenPoiId = picked.poiId;
        if (picked.transport) decision.chosenTransport = picked.transport;
        decisions.push(decision);
      }
    }
  }
  return decisions;
}

// ---------------- 九、确认行程 ----------------
el("confirmBtn").addEventListener("click", async () => {
  hideError("confirmError");
  const decisions = buildDecisions();
  el("decisionsRaw").textContent = JSON.stringify({ decisions: decisions }, null, 2);

  el("confirmBtn").disabled = true;
  el("confirmBtn").textContent = "正在写介绍…";     // 解说要跑一次模型，得等一下
  try {
    const response = await fetch("/api/trips/" + encodeURIComponent(trip.tripId) + "/confirm", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ decisions: decisions }),
    });
    const result = await readJson(response);
    if (!response.ok) throw new ApiError(result);

    el("introList").innerHTML = renderIntros(result.spotIntros || []);
    el("tipList").innerHTML = renderTips(result.travelTips || []);
    el("confirmRaw").textContent = JSON.stringify(result, null, 2);
    el("confirmPanel").hidden = false;
    observeReveals(el("confirmPanel"));
    el("confirmPanel").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (error) {
    showError("confirmError", "确认失败：" + toMessage(error));
  } finally {
    el("confirmBtn").disabled = false;
    el("confirmBtn").textContent = "确认行程";
  }
});

/** 行程里出现过的点 → 照片文件名（解说那块没有图字段，从这里回查） */
function poiPhotos(){
  const map = new Map();
  for (const day of (trip && trip.days) || []){
    for (const segment of day.segments || []){
      for (const item of segment.items || []){
        for (const option of item.options || []){
          if (option.poi && option.poi.photo) map.set(option.poi.poiId, option.poi.photo);
        }
      }
    }
  }
  return map;
}

function renderIntros(intros){
  if (intros.length === 0) return '<div class="dim small">后端没有返回地点介绍</div>';
  const photos = poiPhotos();
  return '<h3 class="section-title">各点介绍</h3><ul class="plain diary-list">' + intros.map((intro) => {
    const photo = photos.get(intro.poiId);
    const thumb = photo
      ? '<img class="diary-thumb" src="/photos/' + esc(encodeURIComponent(photo)) + '" alt="'
        + esc(intro.name) + '" loading="lazy" decoding="async" onerror="this.remove()">'
      : "";
    return "<li>" + thumb + "<b>" + esc(intro.name) + "</b><br>" + esc(intro.intro || "")
      + (intro.sourceDoc ? '<span class="dim small">（出处：' + esc(intro.sourceDoc) + "）</span>" : "")
      + "</li>";
  }).join("") + "</ul>";
}

function renderTips(tips){
  if (tips.length === 0) return '<div class="dim small" style="margin-top:14px">后端没有返回出行建议</div>';
  return '<h3 class="section-title" style="margin-top:16px">出行建议</h3><ul class="plain diary-list">'
       + tips.map((tip) => "<li>" + esc(tip) + "</li>").join("") + "</ul>";
}

// ---------------- 十、启动 ----------------
setupReveal();
buildChips("themeChips", THEMES);
buildChips("avoidChips", AVOIDS);
renderPresets();

el("presetChips").addEventListener("click", (event) => {
  const button = event.target.closest("[data-preset]");
  if (button) applyPreset(PRESETS[Number(button.dataset.preset)]);
});
// 表单里任何改动都重算概要
el("planForm").addEventListener("input", updateSummary);
el("planForm").addEventListener("change", updateSummary);

// 日期默认给个一周后出发、三天行程，省得每次手填
(function prefillDates(){
  const start = new Date();
  start.setDate(start.getDate() + 7);
  const end = new Date(start);
  end.setDate(end.getDate() + 2);
  el("tripStart").value = start.toISOString().slice(0, 10);
  el("tripEnd").value = end.toISOString().slice(0, 10);
})();

// ---------------- 十一、手记（历史旅游方案） ----------------
// 存的是用户确认过的整份行程。详情直接复用现在的渲染函数 —— 它们读的是全局 trip，
// 所以这里临时把 trip 换成存下来的那一份；节点上的切换按钮只绑在 tripDays 上，
// 历史这块的容器没有绑，天然是只读的。

async function loadHistory(){
  el("historyDetailPanel").hidden = true;
  el("historyListPanel").hidden = false;
  el("historyList").innerHTML = '<p class="dim small">正在读…</p>';
  hideError("historyError");

  let items = [];
  try {
    const response = await fetch("/api/plans");
    if (!response.ok) throw new Error("HTTP " + response.status);
    items = await response.json();
  } catch (error) {
    el("historyList").innerHTML = "";
    showError("historyError", "读不到历史方案（" + error.message + "）。");
    return;
  }

  el("historyList").innerHTML = items.length
    ? items.map(renderHistoryCard).join("")
    : '<p class="dim small">还没有确认过的行程。去「路线」生成一份、点确认，就会出现在这里。</p>';
}

function renderHistoryCard(item){
  return '<button type="button" class="plan-card" data-plan="' + esc(item.planId) + '">'
       + '<span class="plan-title">' + esc(item.title || "未命名行程") + "</span>"
       + '<span class="dim small">' + esc(planMetaLine(item)) + "</span></button>";
}

async function openStoredPlan(planId){
  try {
    const response = await fetch("/api/plans/" + encodeURIComponent(planId));
    if (!response.ok) throw new Error("HTTP " + response.status);
    renderStoredPlan(await response.json());
  } catch (error) {
    showError("historyError", "这份方案读不出来（" + error.message + "）。");
  }
}

function renderStoredPlan(plan){
  el("historyListPanel").hidden = true;
  el("historyDetailPanel").hidden = false;
  el("historyTitle").textContent = plan.title || "历史方案";
  el("historyMeta").textContent = plan.destination
    ? plan.destination.province + " " + plan.destination.city : "";

  const current = trip;                 // 借用现有渲染函数（它们读全局 trip）
  trip = plan;
  el("historyBadges").innerHTML = renderBadges();
  el("historyIssues").innerHTML = renderIssues();
  el("historyNotes").innerHTML = renderNotes();
  el("historyIntercity").innerHTML = renderIntercity();
  el("historyLodging").innerHTML = renderLodging();
  el("historyDays").innerHTML = (plan.days || []).map(renderDay).join("");
  trip = current;
  observeReveals(el("historyDetailPanel"));
}

el("historyList").addEventListener("click", (event) => {
  const card = event.target.closest("[data-plan]");
  if (card) openStoredPlan(card.dataset.plan);
});
// 首页的"最近的手记"直接跳到手记那页并打开这一份
el("homeRecent").addEventListener("click", async (event) => {
  const card = event.target.closest("[data-plan]");
  if (!card) return;
  showView("history");
  await openStoredPlan(card.dataset.plan);
});
el("historyBack").addEventListener("click", loadHistory);

updateSummary();                       // 概要跟着默认日期先算一次

showView(viewNameFromHash(), false);   // 进来按地址栏的 # 决定先看哪个视图
loadSpots();
refreshIcons();                        // 把页面上的 <i data-lucide> 换成图标
