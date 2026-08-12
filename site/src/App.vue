<script setup>
import { computed, ref, onMounted, onBeforeUnmount, watch, nextTick } from "vue";

const data = ref(null);
const error = ref(null);
const roadmap = ref(null);

// ---- view state ------------------------------------------------------
const theme = ref("dark"); // dark | light
const mode = ref("fleet"); // fleet | ecosystem
const search = ref("");
const activeCats = ref(new Set());
const showArchived = ref(false);
const collapsed = ref(new Set());
const quick = ref(null); // null | "failing" | "dormant"
const groupBy = ref("category"); // category | tier
const sortKey = ref("stars"); // stars | activity | name

const CAT_LABEL = {
  core: "Core", fabrication: "Fabrication", timber: "Timber", geometry: "Geometry",
  structures: "Structures", fea: "FEA", viz: "Visualization", xr: "XR",
  ai: "AI", apps: "Apps", tooling: "Tooling", template: "Templates", other: "Other",
};
const PY = ["3.9", "3.10", "3.11", "3.12", "3.13"];
const STALE_ORDER = { fresh: 0, aging: 1, stale: 2, dormant: 3, unknown: 4 };
const CAT_RANK = Object.fromEntries(
  ["core", "geometry", "structures", "fea", "fabrication", "timber", "xr", "viz", "ai", "apps", "tooling", "template", "other"].map((c, i) => [c, i])
);
const TIER_GROUP = [
  { id: "core", name: "Core" },
  { id: "foundation", name: "Foundation" },
  { id: "domain", name: "Domain extensions" },
  { id: "visualizers", name: "Visualizers" },
  { id: "apps", name: "Applications" },
  { id: "tooling", name: "Tooling" },
];
const ECO_TIERS = [
  { id: "apps", name: "Applications", sub: "end-user tools & big apps" },
  { id: "visualizers", name: "Visualizers", sub: "viewers, plotters & notebooks" },
  { id: "domain", name: "Domain extensions", sub: "discipline-specific packages" },
  { id: "foundation", name: "Foundation", sub: "geometry & data libraries" },
  { id: "core", name: "Core", sub: "the COMPAS framework" },
  { id: "tooling", name: "Tooling", sub: "shared dev infrastructure" },
];
const VALID_MODES = ["fleet", "ecosystem", "roadmap"];

// ---- lifecycle -------------------------------------------------------
onMounted(async () => {
  try {
    const t = localStorage.getItem("mc_theme");
    if (t) theme.value = t;
    else if (window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches) theme.value = "light";
    const g = localStorage.getItem("mc_group"); if (g) groupBy.value = g;
    const s = localStorage.getItem("mc_sort"); if (s) sortKey.value = s;
  } catch (e) {}
  applyTheme();

  try {
    const [dataRes, roadmapRes] = await Promise.all([
      fetch(`${import.meta.env.BASE_URL}data.json`, { cache: "no-cache" }),
      fetch(`${import.meta.env.BASE_URL}roadmap.json`, { cache: "no-cache" }),
    ]);
    if (!dataRes.ok) throw new Error(`data.json ${dataRes.status}`);
    data.value = await dataRes.json();
    if (roadmapRes.ok) roadmap.value = await roadmapRes.json();
  } catch (e) {
    error.value = String(e);
  }

  applyHash(); // resolve #<mode> or #<repo> now that repo names are known

  const focus = new URLSearchParams(location.search).get("focus");
  if (!selected.value && focus && nameToRepo.value[focus]) {
    mode.value = "ecosystem";
    await nextTick();
    onNodeEnter(focus);
  }
  window.addEventListener("resize", onResize);
  window.addEventListener("hashchange", applyHash);
  window.addEventListener("keydown", onKey);
});
onBeforeUnmount(() => {
  window.removeEventListener("resize", onResize);
  window.removeEventListener("hashchange", applyHash);
  window.removeEventListener("keydown", onKey);
});

function onKey(e) {
  if (e.key === "Escape" && selected.value) closeDetail();
}

watch(theme, applyTheme);
watch(mode, () => {
  hovered.value = null;
  edgeLines.value = [];
});

function applyTheme() {
  document.documentElement.dataset.theme = theme.value;
}
function toggleTheme() {
  theme.value = theme.value === "dark" ? "light" : "dark";
  try { localStorage.setItem("mc_theme", theme.value); } catch (e) {}
}
function setMode(m) { mode.value = m; location.hash = m; }
function setGroup(g) { groupBy.value = g; try { localStorage.setItem("mc_group", g); } catch (e) {} }
function setSort(s) { sortKey.value = s; try { localStorage.setItem("mc_sort", s); } catch (e) {} }
function toggleArchived() { showArchived.value = !showArchived.value; }
function toggleCat(c) {
  const s = new Set(activeCats.value);
  s.has(c) ? s.delete(c) : s.add(c);
  activeCats.value = s;
}
function toggleCollapse(k) {
  const s = new Set(collapsed.value);
  s.has(k) ? s.delete(k) : s.add(k);
  collapsed.value = s;
}
function setQuick(q) { quick.value = quick.value === q ? null : q; }
// ---- helpers ---------------------------------------------------------
function daysSince(ds) {
  if (!ds) return null;
  const d = new Date(ds + "T00:00:00Z");
  return Math.floor((Date.now() - d.getTime()) / 86400000);
}
function relTime(ds) {
  const d = daysSince(ds);
  if (d === null) return "—";
  if (d <= 0) return "today";
  if (d === 1) return "1d";
  if (d < 60) return d + "d";
  if (d < 730) return Math.round(d / 30) + "mo";
  return Math.round(d / 365) + "y";
}
function fmtDate(iso) {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  } catch (e) { return iso; }
}
function pinInfo(r) {
  const raw = r.packaging?.compas_pin;
  if (!raw) return { text: "no pin", cls: "" };
  const spec = raw.replace(/^compas\s*/, "").trim();
  const floor = r.packaging?.compas_major_floor;
  if (floor >= 2) return { text: spec, cls: "pin-2x" };
  if (floor != null && floor < 2) return { text: spec, cls: "pin-old" };
  return { text: spec === "*" || spec === "" ? "unpinned" : spec, cls: "" };
}
const runtime = (r) => r.runtime || "python";
const isPython = (r) => runtime(r) === "python";
const isNode = (r) => runtime(r) === "node";
function runtimeInfo(r) {
  if (isNode(r)) return { label: "Node", value: r.packaging?.node_engine || "unspecified" };
  return { label: r.language || runtime(r), value: "" };
}
function packageManagerInfo(r) {
  const raw = r.packaging?.package_manager;
  return raw ? raw.replace(/@(?=[^@]+$)/, " ") : "unspecified";
}
function repoDistributions(r) {
  if (r.distributions?.length) return r.distributions;
  return r.pypi ? [{ registry: "pypi", name: r.pypi, version: r.release?.pypi_version, url: `https://pypi.org/project/${r.pypi}/` }] : [];
}
const distributionNames = (r) => repoDistributions(r).map((d) => d.registry.toUpperCase()).join(" · ");
const staleClass = (s) => "s-" + (s || "unknown");
const catLabel = (c) => CAT_LABEL[c] || c;
const relTag = (r) => r.release?.github_tag || r.release?.registry_version || r.release?.pypi_version || null;
const relDate = (r) => r.release?.github_date || r.release?.registry_date || r.release?.pypi_date;
function releaseDriftTip(r) {
  const published = repoDistributions(r).filter((d) => d.version).map((d) => `${d.registry} ${d.version}`).join(", ");
  return `GitHub ${r.release?.github_tag || "—"} ≠ ${published || "package registry"}`;
}
const pyOn = (r, v) => (r.packaging?.python_versions || []).includes(v);
const featStatus = (r, id) => r.features?.[id]?.status || "unknown";
function featClass(st) {
  if (st === "adopted") return "adopted";
  if (st === "not-adopted") return "not";
  return "";
}
const applicableFeatures = (r) => features.value.filter((f) => featStatus(r, f.id) !== "n/a");

// ---- derived data ----------------------------------------------------
const repos = computed(() => data.value?.repos ?? []);
const features = computed(() => data.value?.features ?? []);
const categories = computed(() => data.value?.categories ?? []);

const active = computed(() => repos.value.filter((r) => r.status !== "archived"));
const summary = computed(() => {
  const app = active.value.filter((r) => ["adopted", "not-adopted"].includes(r.features?.compas2?.status));
  const on2x = app.filter((r) => r.features?.compas2?.status === "adopted").length;
  return {
    tracked: repos.value.length,
    archived: repos.value.length - active.value.length,
    applicable: app.length, on2x,
    pct2x: app.length ? Math.round((on2x / app.length) * 100) : null,
    failing: active.value.filter((r) => r.health?.ci === "failing").length,
    dormant: active.value.filter((r) => r.health?.staleness === "dormant").length,
    fresh: active.value.filter((r) => r.health?.staleness === "fresh").length,
  };
});

const filtered = computed(() => {
  const q = search.value.trim().toLowerCase();
  const cats = activeCats.value;
  return repos.value.filter((r) => {
    if (!showArchived.value && r.status === "archived") return false;
    if (cats.size && !cats.has(r.category)) return false;
    if (quick.value === "failing" && r.health?.ci !== "failing") return false;
    if (quick.value === "dormant" && r.health?.staleness !== "dormant") return false;
    if (q && !`${r.name} ${r.category} ${r.description ?? ""}`.toLowerCase().includes(q)) return false;
    return true;
  });
});

function comparator(a, b) {
  if ((a.status === "archived") !== (b.status === "archived")) return a.status === "archived" ? 1 : -1;
  if (sortKey.value === "name") return a.name.localeCompare(b.name);
  if (sortKey.value === "activity") {
    const oa = STALE_ORDER[a.health?.staleness] ?? 9, ob = STALE_ORDER[b.health?.staleness] ?? 9;
    if (oa !== ob) return oa - ob;
    return (b.stars ?? 0) - (a.stars ?? 0);
  }
  return (b.stars ?? 0) - (a.stars ?? 0) || a.name.localeCompare(b.name);
}

const lanes = computed(() => {
  const buckets = groupBy.value === "tier"
    ? TIER_GROUP.map((t) => ({ key: t.id, label: t.name, catClass: "", test: (r) => (r.tier || "domain") === t.id }))
    : categories.value.map((c) => ({ key: c, label: catLabel(c), catClass: "cat-" + c, test: (r) => r.category === c }));
  return buckets
    .map((b) => {
      const rs = filtered.value.filter(b.test).slice().sort(comparator);
      if (!rs.length) return null;
      const app = rs.filter((r) => ["adopted", "not-adopted"].includes(r.features?.compas2?.status)).length;
      const on2x = rs.filter((r) => r.features?.compas2?.status === "adopted").length;
      const fail = rs.filter((r) => r.health?.ci === "failing").length;
      return { ...b, repos: rs, app, on2x, fail, open: !collapsed.value.has(b.key) };
    })
    .filter(Boolean);
});
const matchCount = computed(() => filtered.value.length);

// ---- release roadmap -------------------------------------------------
const roadmapStart = computed(() => new Date(`${roadmap.value?.range?.start || "2023-01-01"}T00:00:00Z`));
const roadmapEnd = computed(() => new Date(`${roadmap.value?.range?.end || "2032-01-01"}T00:00:00Z`));
const roadmapAsOf = computed(() => new Date(`${(data.value?.generated_at || new Date().toISOString()).slice(0, 10)}T00:00:00Z`));
const roadmapSpan = computed(() => Math.max(1, roadmapEnd.value - roadmapStart.value));
const roadmapYears = computed(() => {
  const years = [];
  const first = roadmapStart.value.getUTCFullYear();
  const last = roadmapEnd.value.getUTCFullYear();
  for (let year = first; year <= last; year += 1) {
    years.push({ year, left: roadmapPct(`${year}-01-01`) });
  }
  return years;
});
const roadmapTodayLeft = computed(() => roadmapPct(roadmapAsOf.value));

function roadmapPct(value) {
  const date = value instanceof Date ? value : new Date(`${value}T00:00:00Z`);
  return Math.max(0, Math.min(100, ((date - roadmapStart.value) / roadmapSpan.value) * 100));
}
function segmentStyle(segment) {
  const left = roadmapPct(segment.start);
  return { left: `${left}%`, width: `${Math.max(0.35, roadmapPct(segment.end) - left)}%` };
}
function versionSegments(version) {
  if (!version.start_date || version.status === "unreleased") return [];
  if (version.status === "eol") {
    return [{ type: "eol", start: version.start_date, end: version.lifecycle?.eol?.start || version.timeline_end || version.latest_date }];
  }
  const segments = [];
  const feature = version.lifecycle?.feature_development;
  if (feature?.start) segments.push({ type: "feature-development", start: feature.start, end: feature.end || roadmapAsOf.value });
  const lts = version.lifecycle?.lts;
  if (lts?.start) segments.push({ type: "lts", start: lts.start, end: lts.end || lts.max_end || roadmapAsOf.value });
  return segments;
}
function roadmapStatusLabel(status) {
  return roadmap.value?.legend?.find((item) => item.type === status)?.label || status;
}
function fmtRoadmapDate(iso) {
  if (!iso) return "—";
  const date = iso instanceof Date ? iso : new Date(`${iso}T00:00:00Z`);
  return date.toLocaleDateString(undefined, { year: "numeric", month: "short", timeZone: "UTC" });
}

// ---- ecosystem -------------------------------------------------------
const nameToRepo = computed(() => Object.fromEntries(repos.value.map((r) => [r.name, r])));
const dependentsMap = computed(() => {
  const m = {};
  for (const r of repos.value) for (const d of r.ecosystem_deps || []) (m[d] ||= []).push(r.name);
  return m;
});
function ecoNodes(tier) {
  return repos.value
    .filter((r) => (showArchived.value || r.status !== "archived") && (r.tier || "domain") === tier)
    .sort((a, b) => (CAT_RANK[a.category] ?? 99) - (CAT_RANK[b.category] ?? 99) || (b.stars ?? 0) - (a.stars ?? 0) || a.name.localeCompare(b.name));
}
const tierBands = computed(() => ECO_TIERS.map((t) => ({ ...t, repos: ecoNodes(t.id) })).filter((t) => t.repos.length));

function isMatch(r) {
  const q = search.value.trim().toLowerCase();
  if (activeCats.value.size && !activeCats.value.has(r.category)) return false;
  if (q && !`${r.name} ${r.category} ${r.description ?? ""}`.toLowerCase().includes(q)) return false;
  return true;
}
const filterActive = computed(() => !!search.value.trim() || activeCats.value.size > 0);
const hovered = ref(null);
const hoverDeps = computed(() => new Set(hovered.value ? nameToRepo.value[hovered.value]?.ecosystem_deps || [] : []));
const hoverDependents = computed(() => new Set(hovered.value ? dependentsMap.value[hovered.value] || [] : []));
function nodeClass(r) {
  if (hovered.value) {
    if (r.name === hovered.value) return "is-active";
    if (hoverDeps.value.has(r.name)) return "is-dep";
    if (hoverDependents.value.has(r.name)) return "is-dependent";
    return "is-dim";
  }
  if (filterActive.value && !isMatch(r)) return "is-dim";
  return "";
}
const hoveredInfo = computed(() => {
  if (!hovered.value) return null;
  const r = nameToRepo.value[hovered.value];
  return { name: r.name, deps: r.ecosystem_deps || [], dependents: dependentsMap.value[r.name] || [] };
});

const diagramEl = ref(null);
const edgeLines = ref([]);
const diagramSize = ref({ w: 0, h: 0 });
function computeEdges(name) {
  const cont = diagramEl.value;
  if (!cont) return;
  const crect = cont.getBoundingClientRect();
  diagramSize.value = { w: crect.width, h: crect.height };
  const center = (n) => {
    const e = cont.querySelector(`[data-node="${CSS.escape(n)}"]`);
    if (!e) return null;
    const r = e.getBoundingClientRect();
    return { x: r.left + r.width / 2 - crect.left, y: r.top + r.height / 2 - crect.top };
  };
  const from = center(name);
  if (!from) return;
  const lines = [];
  for (const d of nameToRepo.value[name]?.ecosystem_deps || []) {
    const c = center(d);
    if (c) lines.push({ x1: from.x, y1: from.y, x2: c.x, y2: c.y, kind: "dep" });
  }
  for (const d of dependentsMap.value[name] || []) {
    const c = center(d);
    if (c) lines.push({ x1: from.x, y1: from.y, x2: c.x, y2: c.y, kind: "dependent" });
  }
  edgeLines.value = lines;
}
function onNodeEnter(name) {
  hovered.value = name;
  nextTick(() => computeEdges(name));
}
function onNodeLeave() {
  hovered.value = null;
  edgeLines.value = [];
}
function onResize() {
  if (hovered.value) computeEdges(hovered.value);
}

// ---- fast tooltip (replaces the slow native title= on small indicators) --
const tip = ref({ show: false, x: 0, y: 0, text: "" });
function tipShow(e, text) {
  const r = e.currentTarget.getBoundingClientRect();
  tip.value = { show: true, x: r.left + r.width / 2, y: r.top - 8, text };
}
function tipHide() { tip.value.show = false; }
function adoptTip(r, f) {
  const c = r.features?.[f.id] || {};
  const st = c.status || "unknown";
  return c.detail ? `${f.label} — ${st} · ${c.detail}` : `${f.label} — ${st}`;
}

// ---- single-repo detail (deep-linkable via ?repo=<name>) -------------
const selected = ref(null);
const selectedRepo = computed(() => (selected.value ? nameToRepo.value[selected.value] : null));
// Single hash router: #<repo> opens a detail page, #fleet / #ecosystem pick a mode.
function applyHash() {
  const h = decodeURIComponent(location.hash.replace(/^#/, ""));
  if (h && nameToRepo.value[h]) { selected.value = h; return; }
  selected.value = null;
  if (VALID_MODES.includes(h)) mode.value = h;
}
function openDetail(name) {
  selected.value = name;
  location.hash = name;
  window.scrollTo({ top: 0, behavior: "auto" });
}
function closeDetail() {
  selected.value = null;
  location.hash = mode.value;
}
const detailDeps = computed(() => selectedRepo.value?.ecosystem_deps || []);
const detailDependents = computed(() => (selectedRepo.value ? dependentsMap.value[selectedRepo.value.name] || [] : []));
function condaUrl(r) {
  return r?.pypi && r.features?.["conda-forge"]?.status === "adopted"
    ? `https://anaconda.org/conda-forge/${r.pypi}` : null;
}
const DASH_BASE = "https://compas.dev/mission-control/";
const BADGES = [
  { id: "normal", src: "https://compas.dev/badge.svg", label: "Standard" },
  { id: "flat", src: "https://compas.dev/badge-flat.svg", label: "Flat" },
];
function badgeSnippet(r, src) {
  return `[![Made with COMPAS](${src})](${DASH_BASE}#${r.name})`;
}
const copied = ref(null);
async function copyBadge(r, b) {
  try {
    await navigator.clipboard.writeText(badgeSnippet(r, b.src));
    copied.value = b.id;
    setTimeout(() => { if (copied.value === b.id) copied.value = null; }, 1600);
  } catch (e) {}
}
</script>

<template>
  <div v-if="error" class="empty">Failed to load data.json — {{ error }}</div>
  <div v-else-if="!data" class="empty">Loading ecosystem data…</div>

  <template v-else>
    <!-- masthead -->
    <header class="masthead">
      <div class="brand">
        <div class="logo"><div class="logo-dot"></div></div>
        <div>
          <h1 class="brand-title">Mission Control</h1>
          <div class="brand-sub">Signal board · <span class="mono">{{ summary.tracked }}</span> COMPAS repositories</div>
        </div>
      </div>
      <div class="mast-right">
        <div class="collected">
          <div class="collected-label">{{ data.collection_scope === "partial" ? "Partial refresh" : "Collected" }}</div>
          <div class="collected-time mono">{{ fmtDate(data.generated_at) }}</div>
        </div>
        <button class="theme-btn" @click="toggleTheme" title="Toggle theme">{{ theme === "dark" ? "☀" : "☾" }}</button>
      </div>
    </header>

    <!-- ===== SINGLE-REPO DETAIL (deep-linked via ?repo=<name>) ===== -->
    <section v-if="selectedRepo" class="detail">
      <button class="detail-back" @click="closeDetail">← All repositories</button>
      <div class="detail-card">
        <div class="detail-stripe" :style="{ background: `var(--cat-${selectedRepo.category})` }"></div>
        <div class="detail-inner">
          <header class="detail-head">
            <h2 class="detail-name">{{ selectedRepo.name }}<span v-if="selectedRepo.status === 'archived'" class="detail-arch">archived</span></h2>
            <div class="detail-meta">
              <span class="detail-cat"><span class="lane-dot" :style="{ background: `var(--cat-${selectedRepo.category})` }"></span>{{ catLabel(selectedRepo.category) }}</span>
              <span v-if="selectedRepo.role" class="muted">· {{ selectedRepo.role }}</span>
              <span class="mono muted">· {{ selectedRepo.language || runtime(selectedRepo) }}</span>
              <span class="mono muted">· ★ {{ selectedRepo.stars ?? 0 }}</span>
            </div>
          </header>
          <p v-if="selectedRepo.description" class="detail-desc">{{ selectedRepo.description }}</p>

          <div class="detail-signals">
            <div class="dsig"><div class="dsig-label">Activity</div>
              <div class="dsig-val"><span class="dot" :class="staleClass(selectedRepo.health?.staleness)"></span>{{ relTime(selectedRepo.health?.last_commit_date) }} <span class="muted mono">{{ selectedRepo.health?.staleness }}</span></div></div>
            <div class="dsig"><div class="dsig-label">CI</div>
              <div class="dsig-val"><span v-if="selectedRepo.health?.ci === 'passing'" class="ci-pass">● pass</span><span v-else-if="selectedRepo.health?.ci === 'failing'" class="ci-fail">▲ fail</span><span v-else class="ci-none">—</span></div></div>
            <div class="dsig"><div class="dsig-label">Backlog</div>
              <div class="dsig-val mono">{{ selectedRepo.health?.open_issues ?? "—" }} issues · {{ selectedRepo.health?.open_prs ?? "—" }} PRs</div></div>
            <div v-if="isPython(selectedRepo)" class="dsig"><div class="dsig-label">COMPAS pin</div>
              <div class="dsig-val"><span class="pin" :class="pinInfo(selectedRepo).cls">{{ pinInfo(selectedRepo).text }}</span></div></div>
            <div v-else class="dsig"><div class="dsig-label">Runtime</div>
              <div class="dsig-val mono">{{ runtimeInfo(selectedRepo).label }} {{ runtimeInfo(selectedRepo).value }}</div></div>
            <div class="dsig"><div class="dsig-label">Latest release</div>
              <div class="dsig-val mono"><template v-if="relTag(selectedRepo)">{{ relTag(selectedRepo) }} · {{ relTime(relDate(selectedRepo)) }} <span v-if="selectedRepo.release?.drift" class="drift">⚠ drift</span></template><span v-else class="muted">none</span></div></div>
            <div v-if="isPython(selectedRepo)" class="dsig"><div class="dsig-label">Python</div>
              <div class="dsig-val"><span class="py-dots"><span v-for="v in PY" :key="v" class="py-dot" :class="{ on: pyOn(selectedRepo, v) }" @mouseenter="tipShow($event, `Python ${v} — ${pyOn(selectedRepo, v) ? 'supported' : 'not supported'}`)" @mouseleave="tipHide"></span></span></div></div>
            <div v-else-if="isNode(selectedRepo)" class="dsig"><div class="dsig-label">Package manager</div>
              <div class="dsig-val mono">{{ packageManagerInfo(selectedRepo) }}</div></div>
            <div v-if="isPython(selectedRepo)" class="dsig"><div class="dsig-label">Hosts</div>
              <div class="dsig-val hosts"><span class="host" :class="{ on: selectedRepo.packaging?.hosts?.rhino }">R</span><span class="host" :class="{ on: selectedRepo.packaging?.hosts?.ghpython }">GH</span><span class="host" :class="{ on: selectedRepo.packaging?.hosts?.blender }">B</span></div></div>
            <div v-else class="dsig"><div class="dsig-label">Registries</div>
              <div class="dsig-val mono">{{ distributionNames(selectedRepo) || "—" }}</div></div>
          </div>

          <div class="detail-block">
            <div class="detail-block-label">Adoption</div>
            <ul class="adopt-list">
              <li v-for="f in applicableFeatures(selectedRepo)" :key="f.id" class="adopt-item">
                <span class="adot" :class="featClass(featStatus(selectedRepo, f.id))"></span>
                <span class="adopt-name">{{ f.label }}</span>
                <span class="adopt-status" :class="featClass(featStatus(selectedRepo, f.id))">{{ featStatus(selectedRepo, f.id) }}</span>
                <span v-if="selectedRepo.features?.[f.id]?.detail" class="adopt-detail mono">{{ selectedRepo.features[f.id].detail }}</span>
              </li>
            </ul>
          </div>

          <div class="detail-deps">
            <div class="detail-block">
              <div class="detail-block-label">Depends on</div>
              <div class="dep-chips">
                <button v-for="d in detailDeps" :key="d" class="dep-chip" @click="openDetail(d)">{{ d }}</button>
                <span v-if="!detailDeps.length" class="muted">— none tracked</span>
              </div>
            </div>
            <div class="detail-block">
              <div class="detail-block-label">Used by</div>
              <div class="dep-chips">
                <button v-for="d in detailDependents" :key="d" class="dep-chip" @click="openDetail(d)">{{ d }}</button>
                <span v-if="!detailDependents.length" class="muted">— none tracked</span>
              </div>
            </div>
          </div>

          <div class="detail-links">
            <a class="dlink" :href="selectedRepo.url" target="_blank" rel="noopener">GitHub ↗</a>
            <a v-for="d in repoDistributions(selectedRepo)" :key="`${d.registry}:${d.name}`" class="dlink" :href="d.url" target="_blank" rel="noopener">{{ d.registry.toUpperCase() }} ↗</a>
            <a v-if="condaUrl(selectedRepo)" class="dlink" :href="condaUrl(selectedRepo)" target="_blank" rel="noopener">conda-forge ↗</a>
          </div>

          <div class="detail-block badge-block">
            <div class="detail-block-label">Link the “Made with COMPAS” badge to this page</div>
            <div v-for="b in BADGES" :key="b.id" class="badge-row">
              <img :src="b.src" :alt="`Made with COMPAS — ${b.label}`" class="badge-img" />
              <code class="badge-code mono">{{ badgeSnippet(selectedRepo, b.src) }}</code>
              <button class="copy-btn" @click="copyBadge(selectedRepo, b)">{{ copied === b.id ? "✓ copied" : "copy" }}</button>
            </div>
          </div>
        </div>
      </div>
    </section>

    <!-- ===== DASHBOARD ===== -->
    <template v-else>
    <!-- KPI ribbon -->
    <div class="ribbon">
      <div class="kpi">
        <div><span class="kpi-value">{{ summary.tracked }}</span></div>
        <div class="kpi-label">Repositories</div>
        <div class="kpi-sub">{{ summary.archived }} archived</div>
      </div>
      <div class="kpi">
        <div>
          <span class="kpi-value" :class="summary.pct2x === 100 ? 'good' : 'accent'">{{ summary.pct2x === null ? "—" : summary.pct2x }}</span>
          <span class="kpi-unit" v-if="summary.pct2x !== null">%</span>
        </div>
        <div class="kpi-label">On COMPAS 2.x</div>
        <div class="kpi-sub">{{ summary.on2x }}/{{ summary.applicable }}</div>
      </div>
      <button class="kpi clickable" :class="{ active: quick === 'failing' }" @click="setQuick('failing')">
        <div><span class="kpi-value" :class="summary.failing ? 'critical' : 'good'">{{ summary.failing }}</span></div>
        <div class="kpi-label">Failing CI <span class="kpi-cta">{{ quick === "failing" ? "· clear" : "· filter" }}</span></div>
        <div class="kpi-sub">default branch</div>
      </button>
      <button class="kpi clickable" :class="{ active: quick === 'dormant' }" @click="setQuick('dormant')">
        <div><span class="kpi-value" :class="summary.dormant ? 'serious' : ''">{{ summary.dormant }}</span></div>
        <div class="kpi-label">Dormant <span class="kpi-cta">{{ quick === "dormant" ? "· clear" : "· filter" }}</span></div>
        <div class="kpi-sub">&gt;1y idle</div>
      </button>
      <div class="ribbon-mig">
        <div class="mig-head">
          <span class="mig-head-label">2.x adoption</span>
          <span class="mono" style="font-size: 11px; color: var(--muted)">{{ summary.on2x }}/{{ summary.applicable }}</span>
        </div>
        <div class="mig-bar"><div class="mig-bar-fill" :style="{ width: (summary.pct2x || 0) + '%' }"></div></div>
        <div class="mig-legend">
          <span><span style="color: var(--good)">●</span> {{ summary.fresh }} fresh</span>
          <span><span style="color: var(--serious)">●</span> {{ summary.dormant }} dormant</span>
          <span><span style="color: var(--critical)">▲</span> {{ summary.failing }} fail CI</span>
        </div>
      </div>
    </div>

    <!-- toolbar -->
    <div class="toolbar">
      <div class="segmented">
        <button class="seg-btn" :class="{ active: mode === 'fleet' }" @click="setMode('fleet')">Fleet</button>
        <button class="seg-btn" :class="{ active: mode === 'ecosystem' }" @click="setMode('ecosystem')">Ecosystem</button>
        <button class="seg-btn" :class="{ active: mode === 'roadmap' }" @click="setMode('roadmap')">Roadmap</button>
      </div>
      <input v-if="mode !== 'roadmap'" class="search-input" type="search" v-model="search" placeholder="Filter repositories…" />
      <div v-if="mode !== 'roadmap'" class="chips">
        <button v-for="c in categories" :key="c" class="chip" :class="{ active: activeCats.has(c) }" @click="toggleCat(c)">{{ catLabel(c) }}</button>
      </div>
      <div v-if="mode !== 'roadmap'" class="toolbar-right">
        <template v-if="mode === 'fleet'">
          <div class="control">
            <span class="control-label">Group</span>
            <div class="segmented seg-sm">
              <button class="seg-btn" :class="{ active: groupBy === 'category' }" @click="setGroup('category')">Category</button>
              <button class="seg-btn" :class="{ active: groupBy === 'tier' }" @click="setGroup('tier')">Tier</button>
            </div>
          </div>
          <div class="control">
            <span class="control-label">Sort</span>
            <div class="segmented seg-sm">
              <button class="seg-btn" :class="{ active: sortKey === 'stars' }" @click="setSort('stars')">Stars</button>
              <button class="seg-btn" :class="{ active: sortKey === 'activity' }" @click="setSort('activity')">Activity</button>
              <button class="seg-btn" :class="{ active: sortKey === 'name' }" @click="setSort('name')">A–Z</button>
            </div>
          </div>
        </template>
        <label class="toggle"><input type="checkbox" v-model="showArchived" /> Archived</label>
      </div>
    </div>

    <div class="hint-row">
      <span v-if="mode === 'fleet'">each tile = one package · all four signals at a glance</span>
      <span v-else-if="mode === 'ecosystem'">dependency stack — core at the base, applications on top. Hover a package to trace its links.</span>
      <span v-else>major-version lifecycle · release history, support status, and maintenance windows</span>
      <span v-if="mode !== 'roadmap'" class="matchcount">{{ matchCount }} / {{ summary.tracked }} shown</span>
    </div>

    <!-- ===== ECOSYSTEM ===== -->
    <template v-if="mode === 'ecosystem'">
      <div class="eco-caption">
        <template v-if="hoveredInfo">
          <span class="eco-focus">{{ hoveredInfo.name }}</span>
          <span class="eco-rel"><span class="edge-key dep">depends on</span> {{ hoveredInfo.deps.length ? hoveredInfo.deps.join(", ") : "—" }}</span>
          <span class="eco-rel"><span class="edge-key dependent">used by</span> {{ hoveredInfo.dependents.length ? hoveredInfo.dependents.join(", ") : "—" }}</span>
        </template>
        <span v-else class="eco-hint">Hover a package to trace what it depends on and what uses it.</span>
      </div>
      <div class="diagram" ref="diagramEl" @mouseleave="onNodeLeave">
        <svg class="edges" :width="diagramSize.w" :height="diagramSize.h" :viewBox="`0 0 ${diagramSize.w} ${diagramSize.h}`" preserveAspectRatio="none">
          <line v-for="(l, i) in edgeLines" :key="i" :class="l.kind" :x1="l.x1" :y1="l.y1" :x2="l.x2" :y2="l.y2" />
        </svg>
        <div v-for="t in tierBands" :key="t.id" class="tier-band">
          <div class="tier-label">
            <span class="tier-name">{{ t.name }}</span>
            <span class="tier-sub">{{ t.sub }}</span>
          </div>
          <div class="tier-nodes">
            <button
              v-for="r in t.repos" :key="r.name"
              class="node" :class="['cat-' + r.category, nodeClass(r), { archived: r.status === 'archived' }]"
              :data-node="r.name"
              @mouseenter="onNodeEnter(r.name)" @focus="onNodeEnter(r.name)"
              @click="openDetail(r.name)" :title="r.description || r.name"
            >
              <span class="node-dot"></span>
              <span>{{ r.name }}</span>
              <span v-if="r.stars" class="node-stars">★{{ r.stars }}</span>
            </button>
          </div>
        </div>
      </div>
    </template>

    <!-- ===== RELEASE ROADMAP ===== -->
    <template v-else-if="mode === 'roadmap'">
      <section v-if="roadmap" class="roadmap-view">
        <header class="roadmap-head">
          <div>
            <div class="eyebrow">Release lifecycle</div>
            <h2 class="roadmap-title">{{ roadmap.title }}</h2>
            <p class="roadmap-intro">{{ roadmap.description }}</p>
          </div>
        </header>

        <div class="roadmap-legend" aria-label="Product line status legend">
          <span v-for="item in roadmap.legend" :key="item.type"><i :class="`phase-${item.type}`"></i>{{ item.label }}</span>
        </div>

        <div class="roadmap-scroll">
          <div class="roadmap-chart">
            <div v-for="version in roadmap.versions" :key="version.version" class="roadmap-row">
              <div class="roadmap-version">
                <span>{{ version.label }}</span>
                <small class="mono">{{ version.latest || "not released" }}</small>
              </div>
              <div class="roadmap-track">
                <i v-for="tick in roadmapYears" :key="tick.year" class="roadmap-gridline" :style="{ left: tick.left + '%' }"></i>
                <i class="roadmap-today" :style="{ left: roadmapTodayLeft + '%' }"></i>
                <div
                  v-for="(segment, index) in versionSegments(version)" :key="`${version.version}-${segment.type}-${index}`"
                  class="roadmap-phase" :class="`phase-${segment.type}`" :style="segmentStyle(segment)"
                  :title="`${roadmapStatusLabel(segment.type)} · ${fmtRoadmapDate(segment.start)} – ${fmtRoadmapDate(segment.end)}`"
                ></div>
                <i
                  v-if="version.lifecycle?.lts?.start" class="roadmap-lts-separator"
                  :style="{ left: roadmapPct(version.lifecycle.lts.start) + '%' }"
                  :title="`LTS started ${fmtRoadmapDate(version.lifecycle.lts.start)}`"
                ></i>
                <div v-if="version.status === 'unreleased'" class="roadmap-unreleased" :style="{ left: roadmapTodayLeft + '%' }"><span>unreleased</span></div>
              </div>
            </div>
            <div class="roadmap-axis">
              <div class="roadmap-axis-label"></div>
              <div class="roadmap-axis-track">
                <span v-for="tick in roadmapYears" :key="tick.year" :style="{ left: tick.left + '%' }">’{{ String(tick.year).slice(2) }}</span>
                <span class="roadmap-today-label" :style="{ left: roadmapTodayLeft + '%' }">today</span>
              </div>
            </div>
          </div>
        </div>

        <div class="roadmap-table-wrap">
          <table class="roadmap-table">
            <thead><tr><th>Product line</th><th>Lifecycle</th><th>Line started</th><th>Latest release</th><th>Release date</th></tr></thead>
            <tbody>
              <tr v-for="version in roadmap.versions" :key="`${version.version}-details`">
                <td><strong>{{ version.label }}</strong></td>
                <td><span class="status-pill" :class="`phase-${version.status}`">{{ roadmapStatusLabel(version.status) }}</span></td>
                <td class="mono">{{ fmtRoadmapDate(version.start_date) }}</td>
                <td class="mono">{{ version.latest || "—" }}</td>
                <td class="mono">{{ fmtRoadmapDate(version.latest_date) }}</td>
              </tr>
            </tbody>
          </table>
        </div>

        <div class="roadmap-notice">
          <span class="roadmap-notice-tag">Release policy</span>
          <span>{{ roadmap.note }}</span>
          <span class="roadmap-source-links">
            <a v-for="source in roadmap.sources" :key="source.url" :href="source.url" target="_blank" rel="noopener">{{ source.label }} ↗</a>
          </span>
        </div>
      </section>
      <div v-else class="empty">Roadmap data is not available.</div>
    </template>

    <!-- ===== FLEET CARDS ===== -->
    <template v-else>
      <div v-if="!lanes.length" class="empty">No repositories match the filters.</div>
      <div v-for="g in lanes" :key="g.key" class="lane">
        <div class="lane-head" @click="toggleCollapse(g.key)">
          <span class="lane-dot" :class="g.catClass" :style="g.catClass ? { background: 'var(--node-color, var(--accent))' } : { background: 'var(--accent)' }"></span>
          <span class="lane-chev">{{ g.open ? "▾" : "▸" }}</span>
          <span class="lane-name">{{ g.label }}</span>
          <span class="lane-count mono">{{ g.repos.length }}</span>
          <span v-if="g.fail" class="lane-fail">{{ g.fail }} failing</span>
          <span class="lane-2x mono">{{ g.on2x }}/{{ g.app }} on 2.x</span>
        </div>

        <div v-if="g.open" class="card-grid">
          <div v-for="r in g.repos" :key="r.name" class="card clickable" :class="{ archived: r.status === 'archived' }"
               @click="openDetail(r.name)">
            <div class="card-stripe" :style="{ background: `var(--cat-${r.category})` }"></div>
            <div class="card-body">
              <div class="card-head">
                <div class="card-head-left">
                  <a class="card-name" :href="r.url" target="_blank" rel="noopener" @click.stop>{{ r.name }}</a>
                  <span class="card-cat">{{ catLabel(r.category) }}<template v-if="r.role"> · {{ r.role }}</template></span>
                </div>
                <div class="card-head-right">
                  <span class="card-stars">★ {{ r.stars ?? 0 }}</span>
                  <span class="card-permalink" aria-hidden="true">›</span>
                </div>
              </div>

              <div class="stat-grid">
                <div class="stat">
                  <span class="stat-label">Activity</span>
                  <span class="stale-cell"><span class="dot" :class="staleClass(r.health?.staleness)"></span><span class="stat-val">{{ relTime(r.health?.last_commit_date) }}</span></span>
                </div>
                <div class="stat">
                  <span class="stat-label">CI</span>
                  <span v-if="r.health?.ci === 'passing'" class="ci-pass">● pass</span>
                  <span v-else-if="r.health?.ci === 'failing'" class="ci-fail">▲ fail</span>
                  <span v-else class="ci-none">—</span>
                </div>
                <div class="stat">
                  <span class="stat-label">Backlog</span>
                  <span class="stat-val" @mouseenter="tipShow($event, 'open issues · open PRs')" @mouseleave="tipHide">{{ r.health?.open_issues ?? "—" }} · {{ r.health?.open_prs ?? "—" }}</span>
                </div>
              </div>

              <div class="ver-block">
                <div class="ver-row">
                  <span v-if="isPython(r)" class="pin" :class="pinInfo(r).cls" @mouseenter="tipShow($event, 'COMPAS-core pin')" @mouseleave="tipHide">{{ pinInfo(r).text }}</span>
                  <span v-else class="pin" @mouseenter="tipShow($event, `${runtimeInfo(r).label} runtime`)" @mouseleave="tipHide">{{ runtimeInfo(r).label }} {{ runtimeInfo(r).value }}</span>
                  <span v-if="relTag(r)" class="release">{{ relTag(r) }} · {{ relTime(relDate(r)) }}</span>
                  <span v-if="r.release?.drift" class="drift" @mouseenter="tipShow($event, releaseDriftTip(r))" @mouseleave="tipHide">⚠ drift</span>
                </div>
                <div v-if="isPython(r)" class="support-row">
                  <span class="py-dots">
                    <span v-for="v in PY" :key="v" class="py-dot" :class="{ on: pyOn(r, v) }"
                          @mouseenter="tipShow($event, `Python ${v} — ${pyOn(r, v) ? 'supported' : 'not supported'}`)" @mouseleave="tipHide"></span>
                  </span>
                  <span class="hosts">
                    <span class="host" :class="{ on: r.packaging?.hosts?.rhino }" @mouseenter="tipShow($event, `Rhino — ${r.packaging?.hosts?.rhino ? 'yes' : 'no'}`)" @mouseleave="tipHide">R</span>
                    <span class="host" :class="{ on: r.packaging?.hosts?.ghpython }" @mouseenter="tipShow($event, `Grasshopper / GHPython — ${r.packaging?.hosts?.ghpython ? 'yes' : 'no'}`)" @mouseleave="tipHide">GH</span>
                    <span class="host" :class="{ on: r.packaging?.hosts?.blender }" @mouseenter="tipShow($event, `Blender — ${r.packaging?.hosts?.blender ? 'yes' : 'no'}`)" @mouseleave="tipHide">B</span>
                  </span>
                </div>
                <div v-else class="support-row runtime-row">
                  <span class="runtime-support mono">{{ r.packaging?.package_manager ? packageManagerInfo(r) : (r.language || runtime(r)) }}</span>
                  <span class="registry-badges">
                    <span v-for="d in repoDistributions(r)" :key="`${d.registry}:${d.name}`" class="registry-chip">{{ d.registry }}</span>
                  </span>
                </div>
              </div>

              <div class="adoption">
                <div class="adopt-label">Adoption</div>
                <div class="adopt-dots">
                  <span
                    v-for="f in applicableFeatures(r)" :key="f.id"
                    class="adot" :class="featClass(featStatus(r, f.id))"
                    :aria-label="adoptTip(r, f)"
                    @mouseenter="tipShow($event, adoptTip(r, f))" @mouseleave="tipHide"
                  ></span>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </template>
    </template>

    <footer class="pagefoot">COMPAS Mission Control · collected nightly from GitHub &amp; package registries</footer>

    <div v-if="tip.show" class="tooltip mono" :style="{ left: tip.x + 'px', top: tip.y + 'px' }">{{ tip.text }}</div>
  </template>
</template>
