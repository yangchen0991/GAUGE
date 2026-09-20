async (page) => {
  const assert = (condition, message) => {
    if (!condition) throw new Error(message);
  };
  const fresh = () => new Date().toISOString();
  const payload = {
    generated_at: fresh(),
    today: { cost: 0, requests: 0 },
    plan: { window5h_limit: 2000, week_limit: 10000 },
    window5h: { credits: 0, used_pct: 0, reset_eta_min: 90 },
    thisweek: { credits: 2200, used_pct: 22 },
    week: [
      { d: "09-14", full: "2026-09-14", req: 4, cost: 0.82 },
      { d: "09-15", full: "2026-09-15", req: 5, cost: 1.63 },
      { d: "09-16", full: "2026-09-16", req: 6, cost: 2.5 },
      { d: "09-17", full: "2026-09-17", req: 7, cost: 0 },
      { d: "09-18", full: "2026-09-18", req: 8, cost: 4.8 },
      { d: "09-19", full: "2026-09-19", req: 9, cost: 2.76 },
      { d: "09-20", full: "2026-09-20", req: 10, cost: 3.27 }
    ],
    last_error: null
  };

  await page.setViewportSize({ width: 380, height: 460 });
  await page.evaluate((data) => {
    window.__widgetCalls = [];
    window.pywebview = {
      api: {
        start_drag: () => window.__widgetCalls.push(["start_drag"]),
        open_main: () => window.__widgetCalls.push(["open_main"]),
        open_usage_page: () => window.__widgetCalls.push(["open_usage_page"]),
        toggle_layout: () => window.__widgetCalls.push(["toggle_layout"]),
        refresh_now: () => window.__widgetCalls.push(["refresh_now"]),
        hide_widget: () => window.__widgetCalls.push(["hide_widget"]),
        get_widget_settings: () => ({ opacity: 0.75, pinned: false, passthrough: false }),
        update_widget_settings: (patch) => {
          window.__widgetCalls.push(["update_widget_settings", patch]);
        }
      }
    };
    window.renderWidgetMaterial({ glass: false });
    window.renderWidget(data);
  }, payload);

  const initial = await page.evaluate(() => {
    const shell = document.querySelector("#widget").getBoundingClientRect();
    const footer = document.querySelector("footer").getBoundingClientRect();
    const content = document.querySelector("#content");
    const titles = [...document.querySelectorAll("#week rect title")].map((el) => el.textContent);
    return {
      dot: document.querySelector("#dot").className,
      today: document.querySelector("#hd-today").textContent,
      pct5h: document.querySelector("#pct-5h").textContent,
      used5h: document.querySelector("#used-5h").textContent,
      remaining5h: document.querySelector("#remaining-5h").textContent,
      eta: document.querySelector("#eta-5h").textContent,
      status: document.querySelector("#st-text").textContent,
      bg: getComputedStyle(document.querySelector("#widget")).backgroundColor,
      opacity: getComputedStyle(document.querySelector("#widget")).opacity,
      titleCount: titles.length,
      titleText: titles[0],
      footerBottom: footer.bottom,
      shellBottom: shell.bottom,
      scrollHeight: content.scrollHeight,
      clientHeight: content.clientHeight,
      width: shell.width,
      height: shell.height
    };
  });
  assert(initial.dot.includes("ok"), "fresh complete data should be green");
  assert(initial.today.includes("¥0.00") && initial.today.includes("0 次"), "zero values must remain true zero");
  assert(initial.pct5h === "0%", "zero percent must render as 0%");
  assert(initial.used5h.includes("0 / 2,000"), "used/limit must be readable");
  assert(initial.remaining5h === "剩余 2,000", "remaining credits must be shown");
  assert(initial.eta.includes("后开始释放"), "5h ETA must describe release, not reset");
  assert(initial.status.includes("本机已更新"), "fresh status must identify local update");
  assert(initial.titleCount === 7 && initial.titleText.includes("2026-09-14"), "chart tooltips need SVG title nodes");
  assert(initial.opacity === "1", "widget surface must not apply overall opacity");
  assert(initial.footerBottom <= initial.shellBottom + 1, "footer must stay inside the shell");
  assert(initial.scrollHeight <= initial.clientHeight + 1, "normal 380x460 layout must not scroll");

  const material = await page.evaluate(() => {
    const shell = document.querySelector("#widget");
    window.renderWidgetMaterial({ glass: true });
    const glass = getComputedStyle(shell).backgroundColor;
    window.renderWidgetMaterial({ glass: false });
    return { glass, solid: getComputedStyle(shell).backgroundColor, material: shell.dataset.material };
  });
  assert(material.glass.includes("10, 10, 12"), "glass material must use --bg");
  assert(material.solid.includes("16, 16, 20"), "solid material must use #101014");
  assert(material.material === "solid", "material bridge must only select the surface");

  await page.evaluate(() => window.renderWidget({
    generated_at: new Date().toISOString(),
    today: { cost: 1.2, requests: 2 },
    plan: { window5h_limit: 2000, week_limit: 10000 },
    window5h: { credits: 2500, used_pct: 125, reset_eta_min: 0 },
    thisweek: { credits: 0, used_pct: 0 },
    week: []
  }));
  const over = await page.evaluate(() => ({
    pct: document.querySelector("#pct-5h").textContent,
    fill: document.querySelector("#fill-5h").getAttribute("style"),
    eta: document.querySelector("#eta-5h").textContent
  }));
  assert(over.pct === "125%", "percent text must not clamp over 100");
  assert(over.fill.includes("scaleX(1)"), "progress bar may clamp only its visual fill");
  assert(over.eta.includes("即将开始释放"), "zero ETA must still use release wording");

  await page.evaluate(() => window.renderWidget({
    today: { cost: 0, requests: 0 },
    plan: {},
    window5h: { credits: 0 },
    thisweek: {},
    week: []
  }));
  const missing = await page.evaluate(() => ({
    dot: document.querySelector("#dot").className,
    pct: document.querySelector("#pct-5h").textContent,
    title: document.querySelector("#status").title
  }));
  assert(missing.dot.includes("err") && missing.pct === "—", "missing fields must not look synced");
  assert(missing.title.includes("时间未知"), "missing generated_at needs visible explanation");

  await page.evaluate((data) => window.renderWidget(data), payload);
  await page.evaluate(() => window.renderRefreshState({ state: "failed", detail: "demo failure" }));
  const failed = await page.evaluate(() => ({
    used: document.querySelector("#used-5h").textContent,
    error: document.querySelector("#errbar").textContent,
    dot: document.querySelector("#dot").className
  }));
  assert(failed.used.includes("0 / 2,000"), "failed refresh must preserve old values");
  assert(failed.error.includes("demo failure") && failed.dot.includes("err"), "failed refresh needs visible red state");

  await page.evaluate((data) => {
    data.generated_at = new Date(Date.now() - 16 * 60 * 1000).toISOString();
    window.renderWidget(data);
  }, { ...payload, week: payload.week.slice() });
  const stale = await page.evaluate(() => ({
    dot: document.querySelector("#dot").className,
    text: document.querySelector("#st-text").textContent,
    title: document.querySelector("#status").title
  }));
  assert(stale.dot.includes("stale") && stale.text.includes("数据较旧"), "data older than 15 minutes must be marked stale");
  assert(stale.title.includes("超过 15 分钟"), "stale threshold needs a title explanation");

  await page.evaluate(() => window.renderWidgetLayout("wide"));
  const wide = await page.evaluate(() => ({
    className: document.body.className,
    viewBox: document.querySelector("#week").getAttribute("viewBox")
  }));
  assert(wide.className.includes("wx-expanded") && wide.viewBox === "0 0 388 300", "wide layout must use adaptive chart geometry");

  await page.setViewportSize({ width: 360, height: 420 });
  const small = await page.evaluate(() => {
    const shell = document.querySelector("#widget").getBoundingClientRect();
    const footer = document.querySelector("footer").getBoundingClientRect();
    return {
      shellRight: shell.right,
      footerBottom: footer.bottom,
      documentWidth: document.documentElement.scrollWidth,
      viewportWidth: innerWidth
    };
  });
  assert(small.shellRight <= small.viewportWidth + 1, "360px layout must not overflow horizontally");
  assert(small.footerBottom <= 420 + 1, "360x420 layout must keep the footer visible");

  await page.setViewportSize({ width: 380, height: 460 });
  await page.evaluate(() => {
    window.renderWidgetLayout("compact");
    document.querySelector("#btn-gear").click();
  });
  const settingsOpen = await page.evaluate(() => ({
    hidden: document.querySelector("#settings").hidden,
    ariaHidden: document.querySelector("#settings").getAttribute("aria-hidden"),
    active: document.activeElement.id
  }));
  assert(!settingsOpen.hidden && settingsOpen.ariaHidden === "false", "settings dialog must open accessibly");
  assert(settingsOpen.active !== "btn-gear", "focus must move into the open settings dialog");
  await page.keyboard.press("Escape");
  const settingsClosed = await page.evaluate(() => ({
    hidden: document.querySelector("#settings").hidden,
    ariaHidden: document.querySelector("#settings").getAttribute("aria-hidden"),
    active: document.activeElement.id
  }));
  assert(settingsClosed.hidden && settingsClosed.ariaHidden === "true", "hidden settings must be removed from tab flow");
  assert(settingsClosed.active === "btn-gear", "Escape must restore focus to the gear button");

  await page.screenshot({ path: "output/playwright/ui-380x460.png", fullPage: false });
  return {
    status: "PASS",
    initial,
    material,
    over,
    missing,
    failed,
    stale,
    wide,
    small,
    settingsOpen,
    settingsClosed
  };
}
