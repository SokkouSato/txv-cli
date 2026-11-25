'use strict';

/* =========================================================
   本地归档 · 时间线 / 收藏 / 统计
   纯原生 JS；只读 /api/feed、/api/meta、/api/state、/api/stats，
   写收藏、已读和播放进度到本地状态库。
   ========================================================= */

const params = new URLSearchParams(window.location.search);
const PATH = window.location.pathname.replace(/\/+$/, '') || '/';
const TWEET_MATCH = PATH.match(/^\/tweet\/([^/]+)$/);
const AUTHOR_MATCH = PATH.match(/^\/author\/([^/]+)$/);
const TWEET_ID = TWEET_MATCH ? decodeURIComponent(TWEET_MATCH[1]) : '';
const AUTHOR_HANDLE = AUTHOR_MATCH
  ? decodeURIComponent(AUTHOR_MATCH[1]).replace(/^@/, '')
  : '';
const VIEW =
  PATH === '/stats' || PATH === '/stats.html'
    ? 'stats'
    : PATH === '/favorites' || PATH === '/favorites.html'
      ? 'favorites'
      : PATH === '/media' || PATH === '/media.html'
        ? 'media'
        : TWEET_MATCH
          ? 'tweet'
          : AUTHOR_MATCH
            ? 'author'
            : 'timeline';

const DEMO = params.get('demo');
const FEED_VIEWS = ['timeline', 'favorites', 'author'];
const MEDIA_FILTERS = ['all', 'photo', 'video', 'gif'];
const MEDIA_LABELS = {
  all: '全部',
  photo: '图片',
  video: '视频',
  gif: '动图',
};
// 四维正交筛选：媒体类型 × 来源 × 阅读状态（账号维度在头部下拉）。
const MEDIA_TYPES = ['all', 'photo', 'video', 'text'];
const MEDIA_TYPE_LABELS = {
  all: '全部',
  photo: '图片',
  video: '视频',
  text: '文字',
};
const SOURCES = ['all', 'x_bookmark', 'x_like', 'local_fav', 'local_like'];
const SOURCE_LABELS = {
  all: '全部归档',
  x_bookmark: '原站书签',
  x_like: '原站点赞',
  local_fav: '本地收藏',
  local_like: '本地点赞',
};
const READ_STATES = ['all', 'unseen', 'seen'];
const READ_STATE_LABELS = {
  all: '全部',
  unseen: '未读',
  seen: '已读',
};
// 旧的 ?filter= 链接映射到新的正交参数，避免老书签/外链失效。
const LEGACY_FILTERS = {
  video: ['mediaType', 'video'],
  photo: ['mediaType', 'photo'],
  text: ['mediaType', 'text'],
  unseen: ['readState', 'unseen'],
  seen: ['readState', 'seen'],
  bookmark: ['source', 'x_bookmark'],
  like: ['source', 'x_like'],
  fav: ['source', 'local_fav'],
};
// 切筛选时记住上一屏位置，切回同一组合时还原；只用内存，不写 URL。
const ANCHOR_LIMIT = 24;
const SORT_LABELS = {
  created: '发布时间',
  likes: '最多喜欢',
  views: '最多查看',
};
const SORTS = ['created', 'likes', 'views'];
const MARKS_KEY = 'timeline:marks';
const NOTES_KEY = 'timeline:notes';
const HISTORY_KEY = 'timeline:search-history';
const ACCOUNT_PARAM = (params.get('account') || '').trim();

function dateUrlParam(name) {
  const raw = params.get(name) || '';
  return /^\d{4}-\d{2}-\d{2}$/.test(raw) ? raw : '';
}

function positiveUrlParam(name) {
  const raw = params.get(name) || '';
  if (!/^\d+$/.test(raw)) return '';
  const value = Number(raw);
  return value > 0 ? String(value) : '';
}

// 新参数优先；没写新参数时回落到旧 filter= 的映射。
function urlDimensions() {
  const legacyName = (params.get('filter') || '').trim().toLowerCase();
  const legacy = LEGACY_FILTERS[legacyName] || null;
  const pick = (name, allowed) => {
    const raw = (params.get(name) || '').trim().toLowerCase();
    if (allowed.includes(raw)) return raw;
    if (legacy && legacy[0] === name && allowed.includes(legacy[1])) {
      return legacy[1];
    }
    return 'all';
  };
  return {
    mediaType: pick('media_type', MEDIA_TYPES),
    source: pick('source', SOURCES),
    readState: pick('read_state', READ_STATES),
  };
}

const URL_DIMENSIONS = urlDimensions();

const requestedLimit = Number.parseInt(params.get('limit') || '', 10);
const PAGE_SIZE =
  Number.isFinite(requestedLimit) && requestedLimit > 0
    ? Math.min(200, requestedLimit)
    : 30;
const MEDIA_PAGE_SIZE =
  Number.isFinite(requestedLimit) && requestedLimit > 0
    ? Math.min(200, requestedLimit)
    : 36;

const START_OFFSET = (() => {
  const value = Number.parseInt(params.get('start') || '0', 10);
  return Number.isFinite(value) && value > 0 ? value : 0;
})();

if (params.get('freeze') === '1') {
  document.body.classList.add('is-frozen');
}

document.documentElement.dataset.view = VIEW;
document.body.dataset.view = VIEW;

const els = {
  feed: document.getElementById('feed'),
  sentinel: document.getElementById('sentinel'),
  viewTitle: document.getElementById('view-title'),
  headMeta: document.getElementById('head-meta'),
  statsView: document.getElementById('stats-view'),
  accountWrap: document.getElementById('account-wrap'),
  accountSelect: document.getElementById('account-select'),
  tabs: document.getElementById('feed-tabs'),
  mediaTabs: document.getElementById('media-tabs'),
  mediaView: document.getElementById('media-view'),
  authorHeader: document.getElementById('author-header'),
  mediaGrid: document.getElementById('media-grid'),
  mediaSkeleton: document.getElementById('media-skeleton'),
  mediaState: document.getElementById('media-state'),
  mediaSentinel: document.getElementById('media-sentinel'),
  sideMedia: document.getElementById('side-media'),
  mediaBigStat: document.getElementById('media-big-stat'),
  mediaTotal: document.getElementById('media-total'),
  mediaRange: document.getElementById('media-range'),
  mediaSideBar: document.getElementById('media-side-bar'),
  mediaCountPhoto: document.getElementById('media-count-photo'),
  mediaCountVideo: document.getElementById('media-count-video'),
  mediaCountGif: document.getElementById('media-count-gif'),
  mediaMissing: document.getElementById('media-missing'),
  ownerAvatar: document.getElementById('owner-avatar'),
  ownerName: document.getElementById('owner-name'),
  ownerHandle: document.getElementById('owner-handle'),
  sideOwner: document.getElementById('side-owner'),
  sideOwnerName: document.getElementById('side-owner-name'),
  sideOwnerHandle: document.getElementById('side-owner-handle'),
  ownerFollowers: document.getElementById('owner-followers'),
  ownerFollowing: document.getElementById('owner-following'),
  ownerJoined: document.getElementById('owner-joined'),
  ownerFlags: document.getElementById('owner-flags'),
  ownerSync: document.getElementById('owner-sync'),
  mediaOldest: document.getElementById('media-oldest'),
  mediaNewest: document.getElementById('media-newest'),
  mediaGenerated: document.getElementById('media-generated'),
  filterContext: document.getElementById('filter-context'),
  filterAuthor: document.getElementById('filter-author'),
  clearAuthor: document.getElementById('clear-author'),
  searchInputs: Array.from(document.querySelectorAll('[data-search-input]')),
  searchClears: Array.from(document.querySelectorAll('[data-search-clear]')),
  searchHistories: Array.from(document.querySelectorAll('[data-search-history]')),
  filterBtn: document.getElementById('filter-btn'),
  filterCount: document.getElementById('filter-count'),
  filterPanel: document.getElementById('advanced-filter'),
  filterSince: document.getElementById('filter-since'),
  filterUntil: document.getElementById('filter-until'),
  filterMinLikes: document.getElementById('filter-min-likes'),
  filterMinViews: document.getElementById('filter-min-views'),
  filterApply: document.getElementById('filter-apply'),
  filterClear: document.getElementById('filter-clear'),
  bigStat: document.getElementById('big-stat'),
  statTweets: document.getElementById('stat-tweets'),
  sideRange: document.getElementById('stat-range'),
  mediaBar: document.getElementById('media-bar'),
  countPhoto: document.getElementById('count-photo'),
  countVideo: document.getElementById('count-video'),
  countGif: document.getElementById('count-gif'),
  metaOldest: document.getElementById('meta-oldest'),
  metaNewest: document.getElementById('meta-newest'),
  metaGenerated: document.getElementById('meta-generated'),
  settingsSheet: document.getElementById('settings-sheet'),
  settingsBackdrop: document.getElementById('settings-backdrop'),
  themeRows: Array.from(document.querySelectorAll('[data-theme-choice]')),
  autoplaySwitch: document.getElementById('autoplay-switch'),
  motionSwitch: document.getElementById('motion-switch'),
  stateExport: document.getElementById('state-export'),
  stateReset: document.getElementById('state-reset'),
  markAllSeen: document.getElementById('mark-all-seen'),
  localExport: document.getElementById('local-export'),
  localImportTrigger: document.getElementById('local-import-trigger'),
  localImport: document.getElementById('local-import'),
  notesOpenBtn: document.getElementById('notes-open'),
  notesSheet: document.getElementById('notes-sheet'),
  notesBackdrop: document.getElementById('notes-backdrop'),
  notesSearch: document.getElementById('notes-search'),
  notesSearchClear: document.getElementById('notes-search-clear'),
  notesCount: document.getElementById('notes-count'),
  notesList: document.getElementById('notes-list'),
  noteSheet: document.getElementById('note-sheet'),
  noteBackdrop: document.getElementById('note-backdrop'),
  noteContext: document.getElementById('note-context'),
  noteInput: document.getElementById('note-input'),
  noteCount: document.getElementById('note-count'),
  noteDelete: document.querySelector('[data-delete-note]'),
  noteSave: document.querySelector('[data-save-note]'),
  noteCloses: Array.from(document.querySelectorAll('[data-close-note]')),
  lightbox: document.getElementById('lightbox'),
  lightboxStage: document.getElementById('lightbox-stage'),
  lightboxCounter: document.getElementById('lightbox-counter'),
  lightboxDownload: document.getElementById('lightbox-download'),
  lightboxCaption: document.getElementById('lightbox-caption'),
  lightboxClose: document.getElementById('lightbox-close'),
  lightboxPrev: document.getElementById('lightbox-prev'),
  lightboxNext: document.getElementById('lightbox-next'),
  toast: document.getElementById('toast'),
};

const state = {
  view: VIEW,
  // 四维正交筛选：媒体类型 / 来源 / 阅读状态（账号见 account）
  mediaType: URL_DIMENSIONS.mediaType,
  source: VIEW === 'favorites' ? 'local_fav' : URL_DIMENSIONS.source,
  readState: URL_DIMENSIONS.readState,
  sort: SORTS.includes(params.get('sort')) ? params.get('sort') : 'created',
  q: (params.get('q') || '').trim(),
  since: dateUrlParam('since'),
  until: dateUrlParam('until'),
  minLikes: positiveUrlParam('min_likes'),
  minViews: positiveUrlParam('min_views'),
  author:
    VIEW === 'author'
      ? AUTHOR_HANDLE
      : (params.get('author') || '').trim().replace(/^@/, ''),
  authorName: '',
  // 归档账号（meta.accounts）：account 参数刷新可恢复，与 q/author/filter/sort 共存
  accounts: [],
  account: ACCOUNT_PARAM,
  // 归档里出现过的作者 handle（meta.authors，回退到已加载卡片）；
  // 正文 @提及 用它判断「跳作者页」还是「跳本地搜索」。
  knownHandles: new Set(),
  tweetId: TWEET_ID,
  offset: 0,
  loaded: 0,
  total: 0,
  index: 0,
  loading: false,
  done: false,
  started: false,
  generation: 0,
  feedObserver: null,
  meta: null,
  sessionId: null,
  cards: new Map(),
  // 视图锚点缓存：组合键 -> { id, offset, loaded }；只复用完全一致的组合
  anchors: new Map(),
  activeKey: '',

  searchHistory: readSearchHistory(),

  seenObserver: null,
  seenTimers: new Map(),
  seenPending: new Set(),
  seenFlushTimer: null,
  seenSending: false,
  detailGeneration: 0,
  detailChildCount: 0,

  media: {
    filter: MEDIA_FILTERS.includes(params.get('filter'))
      ? params.get('filter')
      : 'all',
    offset: 0,
    loaded: 0,
    total: 0,
    loading: false,
    done: false,
    started: false,
    generation: 0,
    observer: null,
    items: new Map(),
  },

  autoplay: false,
  reduceMotion: false,
  videoEntries: [],
  activeVideo: null,
  videoFlush: [],

  settingsOpen: false,
  settingsTrigger: null,
  settingsTrap: null,
  lightbox: null,
  lightboxTrap: null,
  lightboxArrowHandler: null,
  lightboxCloseTimer: null,
  note: null,
  noteTrap: null,
  notesOpen: false,
  notesTrigger: null,
  notesTrap: null,
  marks: {},
  notes: {},
  moreOpenId: null,
  moreTrigger: null,
  focusedId: null,
  gPending: false,
  gTimer: null,
  toastTimer: null,
  searchTimer: null,
  statsGeneration: 0,
  statsDays: [7, 14, 30, 60].includes(Number(params.get('days')))
    ? Number(params.get('days'))
    : 14,
};

if (state.q && VIEW !== 'author') state.author = '';

state.marks = readLocalStore(MARKS_KEY);
state.notes = readLocalStore(NOTES_KEY);

const initialFeedHtml = els.feed ? els.feed.innerHTML : '';
const initialStatsHtml = els.statsView ? els.statsView.innerHTML : '';

// 高级筛选面板内的暂存选择：面板里点选只改这里，点「应用」才写入 state。
const filterDraft = {
  source: state.source,
  readState: state.readState,
  sort: state.sort,
};

/* =========================================================
   极简 DOM 工具
   ========================================================= */

function h(tag, props, ...children) {
  const node = document.createElement(tag);
  if (props) {
    Object.keys(props).forEach((key) => {
      const value = props[key];
      if (value === null || value === undefined || value === false) return;
      if (key === 'class') node.className = value;
      else if (key === 'text') node.textContent = String(value);
      else if (key === 'dataset') Object.assign(node.dataset, value);
      else if (key.startsWith('on') && typeof value === 'function') {
        node.addEventListener(key.slice(2).toLowerCase(), value);
      } else if (
        tag === 'video' &&
        ['muted', 'autoplay', 'loop', 'controls', 'playsinline'].includes(key)
      ) {
        // 这几个是媒体元素的 IDL 属性；只写 HTML 属性在无头浏览器里
        // 不一定会同步成 muted/autoplay，导致静音自动播放被策略拦掉。
        if (key === 'playsinline') node.playsInline = Boolean(value);
        else node[key] = Boolean(value);
      } else if (value === true) node.setAttribute(key, '');
      else node.setAttribute(key, String(value));
    });
  }
  appendChildren(node, children);
  return node;
}

function appendChildren(node, children) {
  for (const child of children) {
    if (child === null || child === undefined || child === false) continue;
    if (Array.isArray(child)) appendChildren(node, child);
    else if (child.nodeType) node.append(child);
    else node.append(document.createTextNode(String(child)));
  }
}

function textNode(value) {
  return document.createTextNode(String(value));
}

function icon(name, className = 'icon') {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('class', className);
  svg.setAttribute('aria-hidden', 'true');
  const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
  use.setAttribute('href', `#i-${name}`);
  svg.append(use);
  return svg;
}

function emptyMark() {
  const template = document.createElement('template');
  template.innerHTML = `
    <svg class="state-mark" viewBox="0 0 72 72" fill="none" aria-hidden="true">
      <path d="M23 14v44" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>
      <circle cx="23" cy="20" r="4.2" fill="currentColor"/>
      <path d="M34 20h23M34 36h16M34 52h25" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>
    </svg>`;
  return template.content.firstElementChild;
}

function stateBox(kicker, title, body, action) {
  const wrap = h('div', { class: 'state', role: 'status' });
  const inner = h('div', { class: 'state-inner' });
  inner.append(emptyMark());
  inner.append(h('p', { class: 'state-kicker', text: kicker }));
  inner.append(h('h2', { text: title }));
  inner.append(h('p', { text: body }));
  if (action) inner.append(action);
  wrap.append(inner);
  return wrap;
}

function num(value) {
  const v = Number(value);
  if (!Number.isFinite(v)) return '0';
  return new Intl.NumberFormat('zh-CN').format(v);
}

function toCount(value) {
  const v = Math.trunc(Number(value));
  return Number.isFinite(v) && v > 0 ? v : 0;
}

function compactCount(value) {
  const v = toCount(value);
  if (v < 10000) return num(v);
  if (v < 100000000) {
    return `${(v / 10000).toFixed(v < 100000 ? 1 : 0).replace(/\.0$/, '')}万`;
  }
  return `${(v / 100000000).toFixed(1).replace(/\.0$/, '')}亿`;
}

function archiveMetricItems(card) {
  return [
    ['回复', toCount(card.reply_count)],
    ['转推', toCount(card.retweet_count) + toCount(card.quote_count)],
    ['喜欢', toCount(card.like_count)],
    ['收藏', toCount(card.bookmark_count)],
    ['查看', toCount(card.view_count)],
  ].filter((item) => item[1] > 0);
}

function archiveMetricsLine(card) {
  const items = archiveMetricItems(card);
  if (!items.length) return null;
  const line = h('div', {
    class: 'lightbox-metrics',
    'aria-label': items.map(([label, value]) => `${num(value)} ${label}`).join('，'),
  });
  for (const [label, value] of items) {
    line.append(h('span', { text: `${compactCount(value)} ${label}` }));
  }
  return line;
}

function cut(value, max) {
  const s = String(value == null ? '' : value).replace(/\s+/g, ' ').trim();
  const arr = Array.from(s);
  return arr.length > max ? `${arr.slice(0, max).join('')}…` : s;
}

function initialOf(name, handle) {
  const value = (name || handle || '?').trim();
  const first = Array.from(value)[0] || '?';
  return first.toUpperCase();
}

function fillAvatar(node, url, name, handle) {
  if (!node) return node;
  const initial = initialOf(name, handle);
  const src = avatarUrl(url);
  if (!src) {
    node.textContent = initial;
    return node;
  }
  node.textContent = '';
  const image = h('img', {
    class: 'avatar-img',
    src,
    alt: '',
    loading: 'lazy',
    decoding: 'async',
  });
  image.addEventListener(
    'error',
    () => {
      image.remove();
      node.textContent = initial;
    },
    { once: true }
  );
  node.append(image);
  return node;
}

function avatarUrl(value) {
  const raw = String(value == null ? '' : value).trim();
  if (!raw) return '';
  // 导出数据里的 avatar 是文件名，走本机 /avatars/<file>；兼容已经带路径的旧数据
  if (/^https?:\/\//i.test(raw) || raw.startsWith('/')) return raw;
  return `/avatars/${encodeURIComponent(raw)}`;
}

/* ---------- 归档账号 / @提及 ---------- */

function accountTag(id) {
  const wanted = String(id || '').toLowerCase();
  const account = state.accounts.find(
    (item) => String(item.id).toLowerCase() === wanted
  );
  return account && account.handle ? `@${account.handle}` : String(id || '');
}

function rememberHandle(handle) {
  const value = String(handle || '').trim().replace(/^@/, '').toLowerCase();
  if (value) state.knownHandles.add(value);
}

function mentionHref(handle) {
  const clean = String(handle || '').replace(/^@/, '');
  if (!clean) return '/';
  return state.knownHandles.has(clean.toLowerCase())
    ? `/author/${encodeURIComponent(clean)}`
    : `/?q=${encodeURIComponent(`@${clean}`)}`;
}

function applyMetaAccounts(meta) {
  const accounts = Array.isArray(meta && meta.accounts) ? meta.accounts : [];
  state.accounts = accounts.filter((item) => item && item.id);
  for (const account of state.accounts) rememberHandle(account.handle);
  if (Array.isArray(meta && meta.authors)) {
    state.knownHandles = new Set(
      meta.authors
        .map((handle) => String(handle || '').trim().replace(/^@/, '').toLowerCase())
        .filter(Boolean)
    );
  }

  // URL 里的 account 失效（换过归档）时清掉，避免一直显示空列表；
  // meta 整个没取到（离线/演示）时不动，宁可留给服务端返回空列表。
  let changed = false;
  if (meta && state.account && state.accounts.length && !state.accounts.some(
    (item) => String(item.id).toLowerCase() === state.account.toLowerCase()
  )) {
    state.account = '';
    changed = true;
  }
  updateAccountUi();
  return changed;
}

function updateAccountUi() {
  if (!els.accountWrap || !els.accountSelect) return;
  const visible = state.accounts.length > 0 && FEED_VIEWS.includes(VIEW);
  els.accountWrap.hidden = !visible;
  if (!visible) return;
  const select = els.accountSelect;
  const wanted = state.account || '';
  const options = [h('option', { value: '', text: '全部账号' })];
  for (const account of state.accounts) {
    const handle = account.handle ? `@${account.handle}` : account.id;
    const count = Number(account.tweets);
    const detail = Number.isFinite(count) && count > 0
      ? `${num(count)} 条`
      : '';
    const option = h('option', {
      value: account.id,
      text: handle,
      title: [account.name, detail].filter(Boolean).join(' · ') || handle,
    });
    options.push(option);
  }
  select.replaceChildren(...options);
  select.value = wanted;
  if (select.value !== wanted) select.value = '';
  select.title = state.account
    ? `只看 ${accountTag(state.account)} 归档的卡片`
    : '全部账号';
}

function dateKey(date) {
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
}

function monthStamp(iso) {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return '—';
  return `${date.getUTCFullYear()}.${String(date.getUTCMonth() + 1).padStart(2, '0')}`;
}

function dayStamp(iso) {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return '—';
  return `${date.getUTCFullYear()}.${String(date.getUTCMonth() + 1).padStart(2, '0')}.${String(date.getUTCDate()).padStart(2, '0')}`;
}

function yearOf(iso) {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? '' : date.getUTCFullYear();
}

function formatTweetTime(iso) {
  if (!iso) return '';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return '';
  const diff = Date.now() - date.getTime();
  if (diff >= 0 && diff < 60 * 1000) return '刚刚';
  if (diff >= 0 && diff < 60 * 60 * 1000) {
    return `${Math.max(1, Math.floor(diff / (60 * 1000)))} 分钟`;
  }
  if (diff >= 0 && diff < 24 * 60 * 60 * 1000) {
    return `${Math.max(1, Math.floor(diff / (60 * 60 * 1000)))} 小时`;
  }
  const now = new Date();
  const month = date.getMonth() + 1;
  const day = date.getDate();
  if (date.getFullYear() === now.getFullYear()) return `${month}月${day}日`;
  return `${date.getFullYear()}年${month}月${day}日`;
}

function relativeTime(iso) {
  if (!iso) return '—';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return '—';
  const diff = Date.now() - date.getTime();
  if (diff < 0) return '刚刚';
  if (diff < 45 * 1000) return '刚刚';
  if (diff < 60 * 60 * 1000) return `${Math.max(1, Math.floor(diff / 60000))} 分钟前`;
  if (diff < 24 * 60 * 60 * 1000) return `${Math.floor(diff / 3600000)} 小时前`;
  if (diff < 30 * 24 * 60 * 60 * 1000) return `${Math.floor(diff / 86400000)} 天前`;
  if (diff < 365 * 24 * 60 * 60 * 1000) return `${Math.floor(diff / (30 * 86400000))} 个月前`;
  return `${Math.floor(diff / (365 * 86400000))} 年前`;
}

function formatDuration(ms) {
  const value = Number(ms);
  if (!Number.isFinite(value) || value <= 0) return '';
  const total = Math.round(value / 1000);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  if (hours > 0) {
    return `${hours}:${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`;
  }
  return `${minutes}:${String(seconds).padStart(2, '0')}`;
}

function watchedParts(ms) {
  const value = Math.max(0, Number(ms) || 0);
  const seconds = Math.round(value / 1000);
  if (seconds < 60) return { value: num(seconds), unit: '秒' };
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return { value: num(minutes), unit: '分钟' };
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return { value: num(hours), unit: rest ? `小时 ${rest} 分` : '小时' };
}

function watchedShort(ms) {
  const parts = watchedParts(ms);
  return `${parts.value} ${parts.unit}`;
}

function prettyUrl(raw) {
  try {
    const url = new URL(raw);
    const host = url.hostname.replace(/^www\./i, '');
    const path = url.pathname === '/' ? '' : url.pathname;
    const query = url.search || '';
    let compact = `${host}${path}${query}`;
    if (compact.length > 46) compact = `${compact.slice(0, 45)}…`;
    return compact;
  } catch (_) {
    return raw;
  }
}

function escapeRegExp(value) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

function newId() {
  try {
    if (window.crypto && typeof window.crypto.randomUUID === 'function') {
      return window.crypto.randomUUID();
    }
  } catch (_) {
    /* 忽略 */
  }
  return `v-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function readSetting(key, fallback) {
  try {
    const value = localStorage.getItem(key);
    if (value === '1') return true;
    if (value === '0') return false;
  } catch (_) {
    /* 忽略 */
  }
  return fallback;
}

function writeSetting(key, value) {
  try {
    localStorage.setItem(key, value ? '1' : '0');
  } catch (_) {
    /* 忽略 */
  }
}

function readLocalStore(key) {
  try {
    const value = JSON.parse(localStorage.getItem(key) || '{}');
    return value && typeof value === 'object' && !Array.isArray(value) ? value : {};
  } catch (_) {
    return {};
  }
}

function writeLocalStore(key, value) {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch (_) {
    /* 忽略：隐私模式下 localStorage 可能不可写 */
  }
}

function readSearchHistory() {
  try {
    const value = JSON.parse(localStorage.getItem(HISTORY_KEY) || '[]');
    if (!Array.isArray(value)) return [];
    return value
      .filter((item) => typeof item === 'string' && item.trim())
      .map((item) => item.trim())
      .slice(0, 8);
  } catch (_) {
    return [];
  }
}

function writeSearchHistory(value) {
  try {
    localStorage.setItem(HISTORY_KEY, JSON.stringify(value));
  } catch (_) {
    /* 忽略：隐私模式下 localStorage 可能不可写 */
  }
}

function noteFor(tweetId) {
  const note = state.notes[tweetId];
  if (!note || typeof note.text !== 'string' || !note.text.trim()) return null;
  return note;
}

function markFor(tweetId) {
  const mark = state.marks[tweetId];
  return mark && typeof mark === 'object' ? mark : {};
}

function prefersReducedMotion() {
  try {
    return window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  } catch (_) {
    return false;
  }
}

function motionReduced() {
  return Boolean(state.reduceMotion) || prefersReducedMotion();
}

/* 统一的浮层开合：is-open 控制过渡，隐藏延迟 200ms，受 reduce-motion 约束。 */
const popoverTimers = new WeakMap();

function popoverOpen(node) {
  return Boolean(node && !node.hidden);
}

function showPopover(node) {
  if (!node) return;
  const timer = popoverTimers.get(node);
  if (timer) {
    window.clearTimeout(timer);
    popoverTimers.delete(node);
  }
  node.hidden = false;
  window.requestAnimationFrame(() => {
    if (!node.hidden) node.classList.add('is-open');
  });
}

function hidePopover(node, options = {}) {
  if (!node) return;
  const timer = popoverTimers.get(node);
  if (timer) {
    window.clearTimeout(timer);
    popoverTimers.delete(node);
  }
  node.classList.remove('is-open');
  const finish = () => {
    popoverTimers.delete(node);
    node.hidden = true;
  };
  if (options.immediate || motionReduced()) {
    finish();
    return;
  }
  popoverTimers.set(node, window.setTimeout(finish, 200));
}

function sheetOpen(node, backdrop) {
  if (backdrop) {
    backdrop.hidden = false;
    window.requestAnimationFrame(() => backdrop.classList.add('is-open'));
  }
  if (!node) return;
  node.hidden = false;
  window.requestAnimationFrame(() => node.classList.add('is-open'));
}

function sheetClose(node, backdrop, stillOpen) {
  if (backdrop) backdrop.classList.remove('is-open');
  if (node) node.classList.remove('is-open');
  const finish = () => {
    if (typeof stillOpen === 'function' && stillOpen()) return;
    if (node) node.hidden = true;
    if (backdrop) backdrop.hidden = true;
  };
  if (motionReduced()) finish();
  else window.setTimeout(finish, 200);
}

function popIcon(button) {
  if (!button || motionReduced()) return;
  button.classList.remove('is-popping');
  // 强制回流，保证连续点击也能重新播放回弹
  void button.offsetWidth;
  button.classList.add('is-popping');
  window.setTimeout(() => button.classList.remove('is-popping'), 280);
}

/* =========================================================
   网络
   ========================================================= */

async function getJSON(url) {
  const response = await fetch(url, {
    cache: 'no-store',
    headers: { Accept: 'application/json' },
  });
  if (!response.ok) {
    let detail = '';
    try {
      const body = await response.json();
      detail = body && body.error ? String(body.error) : '';
    } catch (_) {
      detail = '';
    }
    const error = new Error(detail || `HTTP ${response.status}`);
    error.status = response.status;
    throw error;
  }
  return response.json();
}

async function postJSON(url, body, options = {}) {
  const response = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    body: JSON.stringify(body),
    keepalive: Boolean(options.keepalive),
  });
  if (!response.ok) {
    let detail = '';
    try {
      const data = await response.json();
      detail = data && data.error ? String(data.error) : '';
    } catch (_) {
      detail = '';
    }
    const error = new Error(detail || `HTTP ${response.status}`);
    error.status = response.status;
    throw error;
  }
  return response.json();
}

// /api/meta 单例：boot 里 loadOwnerProfile、各视图、统计共用同一次请求。
// 失败时允许重试一次（之后由调用方 .catch 兜底），避免每个视图各发一次。
let metaPromise = null;
let metaRetried = false;

function loadMeta() {
  if (!metaPromise) {
    metaPromise = fetchMetaOnce();
  }
  return metaPromise;
}

async function fetchMetaOnce() {
  try {
    return await getJSON('/api/meta');
  } catch (error) {
    if (metaRetried) {
      metaPromise = null;
      throw error;
    }
    metaRetried = true;
    try {
      return await getJSON('/api/meta');
    } catch (retryError) {
      metaPromise = null;
      throw retryError;
    }
  }
}

function beaconJSON(url, body) {
  try {
    if (!navigator.sendBeacon) return false;
    return navigator.sendBeacon(
      url,
      new Blob([JSON.stringify(body)], { type: 'application/json' })
    );
  } catch (_) {
    return false;
  }
}

/* =========================================================
   启动
   ========================================================= */

boot();

function boot() {
  applyStaticUi();
  applySettings();
  bindGlobalEvents();
  updateThemeRows();
  updateAdvancedUi();
  updateAccountUi();
  loadOwnerProfile();

  if (DEMO === 'loading') {
    setDemoLoading();
    return;
  }
  if (DEMO === 'empty') {
    setDemoEmpty();
    return;
  }
  if (DEMO === 'error') {
    setDemoError();
    return;
  }
  if (VIEW === 'stats') {
    loadStats();
    return;
  }
  if (VIEW === 'media') {
    loadMediaInitial();
    return;
  }
  if (VIEW === 'tweet') {
    loadTweetDetail();
    return;
  }
  loadFeedInitial();
}

function applyStaticUi() {
  const titles = {
    timeline: '时间线',
    favorites: '收藏',
    stats: '统计',
    media: '媒体',
    author: '作者',
    tweet: '详情',
  };
  els.viewTitle.textContent = titles[VIEW];
  document.title =
    VIEW === 'timeline'
      ? '时间线 · 本地归档'
      : `${titles[VIEW]} · 本地归档`;

  const navView = VIEW === 'author' || VIEW === 'tweet' ? 'timeline' : VIEW;
  document.querySelectorAll('[data-nav]').forEach((node) => {
    const active = node.dataset.nav === navView;
    node.classList.toggle('is-active', active);
    if (active) node.setAttribute('aria-current', 'page');
    else node.removeAttribute('aria-current');
  });

  updateFilterUi();
  updateMediaTabs();
  syncSearchInputs();
  updateSearchClear();
  updateAuthorContext();

  if (VIEW === 'stats') {
    els.headMeta.textContent = '观看、播放与收藏';
  } else if (VIEW === 'favorites') {
    els.headMeta.textContent = '正在读取收藏';
  } else if (VIEW === 'media') {
    els.headMeta.textContent = '正在读取媒体';
  } else if (VIEW === 'author') {
    els.headMeta.textContent = '正在读取作者';
  } else if (VIEW === 'tweet') {
    els.headMeta.textContent = '正在读取推文';
  } else {
    els.headMeta.textContent = '正在读取归档';
  }

  const searchPlaceholder =
    VIEW === 'author'
      ? `在 @${state.author} 中搜索`
      : '搜索推文、作者或链接';
  els.searchInputs.forEach((input) => {
    input.placeholder = searchPlaceholder;
  });
}

function setDemoLoading() {
  if (VIEW === 'stats') {
    els.headMeta.textContent = '读取统计…';
    return;
  }
  if (VIEW === 'media') {
    els.headMeta.textContent = '正在读取媒体';
    renderMediaSideLoading();
    return;
  }
  els.headMeta.textContent = VIEW === 'favorites' ? '正在读取收藏' : '正在读取归档';
  els.feed.setAttribute('aria-busy', 'true');
  renderSideLoading();
}

function setDemoEmpty() {
  if (VIEW === 'stats') {
    els.headMeta.textContent = '0 条记录';
    renderStats(emptyStatsData());
    return;
  }
  if (VIEW === 'media') {
    els.headMeta.textContent = '0 个媒体';
    renderMediaSideEmpty();
    renderMediaEmpty('all');
    return;
  }
  els.headMeta.textContent = VIEW === 'favorites' ? '0 条收藏' : '0 条推文 · 暂无内容';
  renderSideEmpty();
  renderEmpty(VIEW === 'favorites' ? 'favorites' : 'all');
}

function setDemoError() {
  if (VIEW === 'stats') {
    renderStatsError(new Error('演示：无法连接本地服务'));
    return;
  }
  if (VIEW === 'media') {
    renderMediaSideLoading();
    renderMediaError(new Error('演示：无法连接本地服务'));
    return;
  }
  renderSideLoading();
  els.sideRange.textContent = '读取失败';
  renderFeedError(new Error('演示：无法连接本地服务'));
}

/* =========================================================
   Feed
   ========================================================= */

async function loadFeedInitial() {
  const metaPromise = loadMeta().catch(() => null);
  const sessionPromise = getJSON('/api/state').catch(() => null);
  const authorPromise =
    VIEW === 'author' && state.author
      ? getJSON(`/api/author?handle=${encodeURIComponent(state.author)}`).catch(
          () => null
        )
      : null;

  let page;
  try {
    page = await getJSON(feedUrl(START_OFFSET));
  } catch (error) {
    renderSideLoading();
    renderFeedError(error);
    return;
  }

  const [meta, session, author] = await Promise.all([
    metaPromise,
    sessionPromise,
    authorPromise,
  ]);
  state.meta = meta;
  state.sessionId = session && session.session_id ? session.session_id : null;
  if (applyMetaAccounts(meta) && ACCOUNT_PARAM) {
    // URL 里的账号不在这个归档里：清掉后重新取一次第一页
    reloadFeed();
    return;
  }
  state.total = Number(page.total) || 0;
  state.offset = START_OFFSET;
  state.loaded = START_OFFSET;
  state.index = START_OFFSET;

  if (VIEW === 'author') {
    state.authorName = (author && author.name) || '';
    renderAuthorHeader(author, page);
  } else if (els.authorHeader) {
    els.authorHeader.hidden = true;
    els.authorHeader.replaceChildren();
  }

  renderSide(meta, page);
  updateHead();

  if (!page.items || page.items.length === 0) {
    renderEmpty(emptyKind());
    return;
  }

  appendItems(page.items);
  state.offset = START_OFFSET + page.items.length;
  state.loaded = START_OFFSET + page.items.length;
  state.activeKey = feedViewKey();

  if (state.loaded >= state.total) finishFeed();
  else setupFeedObserver();

  setupSeenObserver();

  if (params.get('jump')) {
    window.requestAnimationFrame(() => {
      window.requestAnimationFrame(() => jumpToMedia(params.get('jump')));
    });
    return;
  }
  await restoreFeedAnchor();
}

function feedUrl(offset) {
  const query = new URLSearchParams({
    offset: String(offset),
    limit: String(PAGE_SIZE),
    sort: state.sort,
  });
  // 收藏页固定本地收藏；同时带 filter=fav 兼容旧后端，source 给新后端。
  if (VIEW === 'favorites') query.set('filter', 'fav');
  if (state.source !== 'all') query.set('source', state.source);
  if (state.mediaType !== 'all') query.set('media_type', state.mediaType);
  if (state.readState !== 'all') query.set('read_state', state.readState);
  if (state.q) query.set('q', state.q);
  if (state.author) query.set('author', state.author);
  if (state.account) query.set('account', state.account);
  appendAdvancedParams(query);
  return `/api/feed?${query.toString()}`;
}

function appendAdvancedParams(query) {
  if (state.since) query.set('since', state.since);
  if (state.until) query.set('until', state.until);
  if (state.minLikes) query.set('min_likes', state.minLikes);
  if (state.minViews) query.set('min_views', state.minViews);
}

/* ---------- 视图锚点缓存（切筛选后能回到原来的位置） ---------- */

function feedViewKey() {
  return [
    VIEW,
    state.account || '',
    state.source,
    state.mediaType,
    state.readState,
    state.sort,
    state.q || '',
    state.author || '',
    state.since || '',
    state.until || '',
    state.minLikes || '',
    state.minViews || '',
  ].join('|');
}

function firstVisibleArticle() {
  if (!els.feed) return null;
  const articles = els.feed.querySelectorAll('article.tweet');
  for (const article of articles) {
    const rect = article.getBoundingClientRect();
    if (rect.bottom > 96) return article;
  }
  return null;
}

function captureFeedAnchor() {
  if (!state.activeKey || VIEW === 'stats' || VIEW === 'tweet') return;
  const article = firstVisibleArticle();
  if (!article || !article.dataset.tweetId) return;
  state.anchors.set(state.activeKey, {
    id: article.dataset.tweetId,
    offset: Math.round(article.getBoundingClientRect().top),
    loaded: Math.max(state.loaded, 1),
  });
  while (state.anchors.size > ANCHOR_LIMIT) {
    const oldest = state.anchors.keys().next().value;
    state.anchors.delete(oldest);
  }
}

async function ensureFeedLoaded(count) {
  const generation = state.generation;
  let guard = 0;
  while (
    state.loaded < count &&
    !state.done &&
    generation === state.generation &&
    guard < 30
  ) {
    guard += 1;
    const page = await getJSON(feedUrl(state.offset));
    if (generation !== state.generation) return;
    state.total = Number(page.total) || state.total;
    const items = page.items || [];
    if (!items.length) {
      finishFeed();
      break;
    }
    appendItems(items);
    state.offset += items.length;
    state.loaded += items.length;
    if (state.loaded >= state.total) finishFeed();
    else els.sentinel.replaceChildren();
  }
  if (generation === state.generation) updateHead();
}

async function restoreFeedAnchor() {
  const anchor = state.anchors.get(feedViewKey());
  if (!anchor) {
    window.scrollTo({ top: 0, behavior: 'auto' });
    return;
  }
  state.loading = true;
  try {
    await ensureFeedLoaded(anchor.loaded);
  } catch (_) {
    /* 拉取补页失败：保持首屏，用户仍可手动滚动 */
  } finally {
    state.loading = false;
  }
  const target = document.getElementById(`tweet-${anchor.id}`);
  if (!target) return;
  window.requestAnimationFrame(() => {
    window.requestAnimationFrame(() => {
      const node = document.getElementById(`tweet-${anchor.id}`);
      if (!node) return;
      const top = node.getBoundingClientRect().top + window.scrollY - anchor.offset;
      window.scrollTo({ top: Math.max(0, top), behavior: 'auto' });
    });
  });
}

function renderAuthorHeader(summary, page) {
  if (!els.authorHeader) return;
  const handle = state.author;
  const first = page && page.items && page.items.length ? page.items[0] : null;
  const name = (summary && summary.name) || (first && first.name) || handle;
  const tweets = summary ? summary.tweets : Number(page && page.total) || 0;
  const media = summary ? summary.media : 0;
  const oldest = summary && summary.oldest;
  const newest = summary && summary.newest;
  const bio = summary && summary.bio;
  const followers = summary ? summary.followers : null;
  const following = summary ? summary.following : null;
  const joined = summary ? summary.joined : null;
  const location = summary ? summary.location : null;
  const verified = Boolean(summary && summary.verified);

  const avatar = h('span', {
    class: 'avatar author-avatar',
    'aria-hidden': 'true',
  });
  fillAvatar(
    avatar,
    (summary && summary.avatar) || (first && first.avatar),
    name,
    handle
  );
  const copy = h('div', { class: 'author-profile-copy' });

  const nameRow = h('div', { class: 'author-name-row' });
  nameRow.append(h('h2', { class: 'author-name', text: name }));
  if (verified) {
    const badge = h('span', { class: 'author-verified', title: '认证账号' });
    badge.append(icon('check', 'icon author-verified-icon'), h('span', { text: '认证' }));
    nameRow.append(badge);
  }
  copy.append(nameRow);
  copy.append(h('p', { class: 'author-handle', text: `@${handle}` }));

  if (bio) {
    const bioEl = h('p', { class: 'author-bio' });
    renderRichText(bioEl, bio, []);
    copy.append(bioEl);
  }

  const metaBits = [];
  if (location) metaBits.push(location);
  if (joined) metaBits.push(`加入于 ${formatJoined(joined)}`);
  if (metaBits.length) {
    copy.append(h('p', { class: 'author-meta', text: metaBits.join(' · ') }));
  }
  copy.append(h('p', { class: 'author-note', text: '本地归档作者' }));

  const links = h('div', { class: 'author-links' });
  links.append(
    h('a', { class: 'author-link', href: '/', text: '返回时间线' }),
    h('a', {
      class: 'author-link',
      href: `/media?author=${encodeURIComponent(handle)}`,
      text: '查看媒体',
    })
  );

  const stats = h('div', { class: 'author-stats' });
  const addStat = (value, label, extra = '') => {
    const item = h('div', { class: `author-stat${extra ? ` ${extra}` : ''}` });
    item.append(h('strong', { text: value }));
    item.append(h('span', { text: label }));
    stats.append(item);
  };
  addStat(num(tweets), '条推文');
  addStat(num(media), '个媒体');
  if (followers != null) {
    addStat(num(followers), '粉丝');
  }
  if (following != null) {
    addStat(num(following), '关注');
  }
  if (joined) {
    addStat(formatJoined(joined), '加入', 'is-range');
  }
  if (oldest && newest) {
    addStat(`${monthStamp(oldest)} — ${monthStamp(newest)}`, '时间范围', 'is-range');
  }

  const head = h('div', { class: 'author-profile-head' }, avatar, copy, links);
  const profile = h('div', { class: 'author-profile' }, head, stats);
  els.authorHeader.hidden = false;
  els.authorHeader.replaceChildren(profile);
}

function loadTweetDetail() {
  if (!state.tweetId) {
    renderTweetDetailError(new Error('缺少推文 ID'));
    return;
  }
  state.detailGeneration = (state.detailGeneration || 0) + 1;
  const generation = state.detailGeneration;
  const metaPromise = loadMeta().catch(() => null);
  els.headMeta.textContent = '正在读取推文';
  els.feed.setAttribute('aria-busy', 'true');
  els.feed.innerHTML = initialFeedHtml;

  getJSON(`/api/tweet?id=${encodeURIComponent(state.tweetId)}`)
    .then(async (data) => {
      if (generation !== state.detailGeneration) return;
      const meta = await metaPromise;
      if (generation !== state.detailGeneration) return;
      state.meta = meta;
      applyMetaAccounts(meta);
      if (meta) renderSide(meta, { total: 0, items: [] });
      renderTweetDetail(data);
    })
    .catch((error) => {
      if (generation === state.detailGeneration) renderTweetDetailError(error);
    });
}

function renderTweetDetail(data) {
  if (!data || !data.tweet) {
    renderTweetDetailError(new Error('推文不存在'));
    return;
  }
  const all = [data.parent, data.tweet, data.quoted, ...(data.children || [])].filter(
    Boolean
  );
  for (const card of all) {
    rememberHandle(card.handle);
    state.cards.set(card.id, card);
  }
  state.total = all.length;
  state.authorName = data.tweet.name || '';
  state.detailChildCount = Number(data.child_total) || (data.children || []).length;

  const wrapper = h('div', { class: 'detail-view' });
  const toolbar = h('div', { class: 'detail-toolbar' });
  const back = h('a', { class: 'detail-back', href: '/' });
  back.append(icon('arrow-left'), h('span', { text: '返回时间线' }));
  toolbar.append(back);
  wrapper.append(toolbar);

  let index = 0;
  const section = (label, card, extraClass = '') => {
    if (!card) return;
    const node = h('section', {
      class: `detail-section${extraClass ? ` ${extraClass}` : ''}`,
    });
    if (label) node.append(h('h2', { class: 'detail-label', text: label }));
    node.append(renderTweet(card, index++, all.length));
    wrapper.append(node);
  };

  section('上一条', data.parent);
  section('推文', data.tweet, 'is-main');
  section('引用', data.quoted);
  if (data.children && data.children.length) {
    const thread = h('section', { class: 'detail-section detail-thread' });
    const label =
      data.children.length === 1 ? '线程' : `线程 · ${data.children.length}`;
    thread.append(h('h2', { class: 'detail-label', text: label }));
    for (const child of data.children) {
      thread.append(renderTweet(child, index++, all.length));
    }
    wrapper.append(thread);
  }

  els.feed.replaceChildren(wrapper);
  els.feed.setAttribute('aria-busy', 'false');
  updateHead();
  window.scrollTo({ top: 0, behavior: 'auto' });
}

function renderTweetDetailError(error) {
  els.feed.setAttribute('aria-busy', 'false');
  els.feed.replaceChildren(
    stateBox(
      '读取失败',
      '这条推文没有读出来',
      `本地服务没有返回这条推文。${error && error.message ? `（${error.message}）` : ''}`,
      h('a', { class: 'state-action', href: '/', text: '返回时间线' })
    )
  );
  els.headMeta.textContent = '读取失败';
}

function reloadFeed() {
  if (VIEW === 'stats' || VIEW === 'tweet') return;
  // 先把当前这一屏的位置记到旧组合键下（activeKey 是上一次成功渲染的组合）。
  captureFeedAnchor();
  const anchor = state.anchors.get(feedViewKey());
  closeTweetMenus();
  state.focusedId = null;
  state.generation += 1;
  state.offset = 0;
  state.loaded = 0;
  state.total = 0;
  state.index = 0;
  state.loading = false;
  state.done = false;
  state.started = false;
  state.cards.clear();
  if (state.feedObserver) {
    state.feedObserver.disconnect();
    state.feedObserver = null;
  }
  resetSeenObserver();
  resetVideos();
  els.feed.innerHTML = initialFeedHtml;
  els.feed.setAttribute('aria-busy', 'true');
  els.sentinel.replaceChildren();
  els.headMeta.textContent =
    VIEW === 'favorites' ? '正在读取收藏' : '正在读取时间线';
  updateFilterUi();
  updateAuthorContext();
  syncUrl();
  // 命中锚点时不要回到顶部，等第一页回来后再定位到那条推文
  if (!anchor) window.scrollTo({ top: 0, behavior: 'auto' });
  loadFeedPage(0, true);
}

async function loadFeedPage(offset, reset) {
  const generation = state.generation;
  state.loading = true;

  try {
    const page = await getJSON(feedUrl(offset));
    if (generation !== state.generation) return;

    state.total = Number(page.total) || 0;
    if (reset) {
      state.offset = offset;
      state.loaded = offset;
      state.index = offset;
      state.started = false;
    }

    if (!page.items || page.items.length === 0) {
      updateHead();
      renderEmpty(emptyKind());
      return;
    }

    appendItems(page.items);
    state.offset = offset + page.items.length;
    state.loaded = offset + page.items.length;
    updateHead();

    if (reset) {
      state.activeKey = feedViewKey();
      await restoreFeedAnchor();
    }

    if (state.loaded >= state.total || page.items.length === 0) {
      finishFeed();
    } else {
      setupFeedObserver();
    }
    setupSeenObserver();
  } catch (error) {
    if (generation === state.generation) {
      renderFeedError(error);
    }
  } finally {
    state.loading = false;
  }
}

function appendItems(items) {
  if (!state.started) {
    els.feed.replaceChildren();
    state.started = true;
  }

  const fragment = document.createDocumentFragment();
  for (const card of items) {
    if (!card || card.id == null) continue;
    state.index += 1;
    rememberHandle(card.handle);
    state.cards.set(card.id, card);
    fragment.append(renderTweet(card, state.index));
  }
  els.feed.append(fragment);
  els.feed.setAttribute('aria-busy', 'false');
}

function setupFeedObserver() {
  if (!('IntersectionObserver' in window)) {
    loadMore();
    return;
  }
  if (state.feedObserver) state.feedObserver.disconnect();
  state.feedObserver = new IntersectionObserver(
    (entries) => {
      if (entries.some((entry) => entry.isIntersecting)) loadMore();
    },
    { rootMargin: '700px 0px' }
  );
  state.feedObserver.observe(els.sentinel);
}

async function loadMore() {
  if (state.loading || state.done) return;
  state.loading = true;
  const generation = state.generation;
  els.sentinel.replaceChildren(h('div', { class: 'load-more', text: '正在读取更多…' }));

  try {
    const page = await getJSON(feedUrl(state.offset));
    if (generation !== state.generation) return;

    state.total = Number(page.total) || state.total;
    appendItems(page.items || []);
    state.offset += (page.items || []).length;
    state.loaded += (page.items || []).length;

    if (state.loaded >= state.total || (page.items || []).length === 0) {
      finishFeed();
    } else {
      els.sentinel.replaceChildren();
    }
    updateHead();
  } catch (error) {
    if (generation === state.generation) renderLoadMoreError(error);
  } finally {
    state.loading = false;
  }
}

function finishFeed() {
  state.done = true;
  if (state.feedObserver) {
    state.feedObserver.disconnect();
    state.feedObserver = null;
  }
  els.feed.setAttribute('aria-busy', 'false');
  els.sentinel.replaceChildren(
    h(
      'div',
      { class: 'feed-end' },
      h('span', { text: `已显示全部 ${num(state.total)} 条` })
    )
  );
}

function updateFeedEndLabel() {
  const label = els.sentinel.querySelector('.feed-end span');
  if (label) label.textContent = `已显示全部 ${num(state.total)} 条`;
}

function renderLoadMoreError(error) {
  const retry = h('button', {
    type: 'button',
    class: 'state-action',
    text: '重试',
    onclick: () => {
      els.sentinel.replaceChildren();
      loadMore();
    },
  });
  const box = h(
    'div',
    { class: 'load-more', role: 'alert' },
    h('span', { text: `读取下一页失败：${error.message}` }),
    h('br'),
    retry
  );
  els.sentinel.replaceChildren(box);
}

function emptyKind() {
  if (state.q || state.author) return 'search';
  if (VIEW === 'favorites') return 'favorites';
  if (
    state.mediaType !== 'all' ||
    state.source !== 'all' ||
    state.readState !== 'all' ||
    state.account
  ) {
    return 'filter';
  }
  return 'all';
}

function renderEmpty(kind) {
  els.feed.setAttribute('aria-busy', 'false');
  els.sentinel.replaceChildren();

  let kicker = '归档为空';
  let title = '时间线还是空的';
  let body = '这个归档里还没有可显示的内容。';
  let action = null;

  if (kind === 'favorites') {
    kicker = '收藏为空';
    title = '还没有收藏';
    body = '在时间线里点书签图标，收藏会出现在这里。';
    action = h('a', { class: 'state-action', href: '/', text: '回到时间线' });
  } else if (kind === 'search') {
    kicker = '没有结果';
    title = '没有找到匹配的推文';
    body = '换个关键词，或者清除搜索和筛选再看。';
    action = h('button', {
      type: 'button',
      class: 'state-action',
      text: '清除筛选',
      onclick: clearFilters,
    });
  } else if (kind === 'filter') {
    kicker = '筛选为空';
    title = '这个筛选下没有内容';
    body = '换一个筛选条件，或者回到全部时间线。';
    action = h('button', {
      type: 'button',
      class: 'state-action',
      text: '查看全部',
      onclick: clearFilters,
    });
  }

  els.feed.replaceChildren(stateBox(kicker, title, body, action));
}

function renderFeedError(error) {
  els.feed.setAttribute('aria-busy', 'false');
  els.sentinel.replaceChildren();
  const retry = h('button', {
    type: 'button',
    class: 'state-action',
    text: '重新加载',
    onclick: () => {
      els.feed.innerHTML = initialFeedHtml;
      loadFeedInitial();
    },
  });
  els.feed.replaceChildren(
    stateBox(
      '读取失败',
      '时间线没有读出来',
      `本地服务没有返回可用的数据。${error && error.message ? `（${error.message}）` : ''}`,
      retry
    )
  );
  els.headMeta.textContent = '读取失败';
}

/* =========================================================
   媒体墙
   ========================================================= */

function mediaUrl(offset) {
  const query = new URLSearchParams({
    offset: String(offset),
    limit: String(MEDIA_PAGE_SIZE),
    filter: state.media.filter,
    sort: state.sort,
  });
  if (state.q) query.set('q', state.q);
  if (state.author) query.set('author', state.author);
  appendAdvancedParams(query);
  return `/api/media?${query.toString()}`;
}

async function loadMediaInitial() {
  const generation = state.media.generation;
  const metaPromise = loadMeta().catch(() => null);

  let page;
  try {
    page = await getJSON(mediaUrl(0));
  } catch (error) {
    if (generation !== state.media.generation) return;
    renderMediaSideLoading();
    renderMediaError(error);
    return;
  }

  const meta = await metaPromise;
  if (generation !== state.media.generation) return;
  state.meta = meta;
  state.media.total = Number(page.total) || 0;
  state.media.offset = 0;
  state.media.loaded = 0;
  state.media.loading = false;
  state.media.done = false;
  state.media.started = false;
  state.media.items.clear();

  renderMediaSide(meta, page);
  updateMediaHead();

  if (!page.items || page.items.length === 0) {
    renderMediaEmpty(mediaEmptyKind());
    return;
  }

  appendMediaItems(page.items);
  state.media.offset = page.items.length;
  state.media.loaded = page.items.length;

  if (state.media.loaded >= state.media.total) finishMedia();
  else setupMediaObserver();
}

function reloadMedia() {
  if (VIEW !== 'media') return;
  state.media.generation += 1;
  state.media.offset = 0;
  state.media.loaded = 0;
  state.media.total = 0;
  state.media.loading = false;
  state.media.done = false;
  state.media.started = false;
  state.media.items.clear();
  if (state.media.observer) {
    state.media.observer.disconnect();
    state.media.observer = null;
  }
  els.mediaGrid.replaceChildren();
  els.mediaGrid.hidden = false;
  els.mediaState.hidden = true;
  els.mediaState.replaceChildren();
  els.mediaSkeleton.hidden = false;
  els.mediaSentinel.replaceChildren();
  els.mediaView.setAttribute('aria-busy', 'true');
  els.headMeta.textContent = '正在读取媒体';
  updateMediaTabs();
  updateAuthorContext();
  syncUrl();
  window.scrollTo({ top: 0, behavior: 'auto' });
  loadMediaPage(0, true);
}

async function loadMediaPage(offset, reset) {
  const generation = state.media.generation;
  state.media.loading = true;

  try {
    const page = await getJSON(mediaUrl(offset));
    if (generation !== state.media.generation) return;

    state.media.total = Number(page.total) || 0;
    if (reset) {
      state.media.offset = offset;
      state.media.loaded = offset;
      state.media.started = false;
    }

    if (!page.items || page.items.length === 0) {
      updateMediaHead();
      renderMediaEmpty(mediaEmptyKind());
      return;
    }

    appendMediaItems(page.items);
    state.media.offset = offset + page.items.length;
    state.media.loaded = offset + page.items.length;
    updateMediaHead();

    if (state.media.loaded >= state.media.total) finishMedia();
    else setupMediaObserver();
  } catch (error) {
    if (generation === state.media.generation) renderMediaError(error);
  } finally {
    if (generation === state.media.generation) state.media.loading = false;
  }
}

function appendMediaItems(items) {
  if (!state.media.started) {
    els.mediaSkeleton.hidden = true;
    els.mediaState.hidden = true;
    els.mediaState.replaceChildren();
    els.mediaGrid.hidden = false;
    state.media.started = true;
  }

  const fragment = document.createDocumentFragment();
  for (const item of items) {
    if (!item || !item.media_key) continue;
    state.media.items.set(item.media_key, item);
    fragment.append(renderMediaTile(item));
  }
  els.mediaGrid.append(fragment);
  els.mediaView.setAttribute('aria-busy', 'false');
}

function mediaKindLabel(kind) {
  return kind === 'video' ? '视频' : kind === 'animated_gif' ? '动图' : '图片';
}

function mediaItemSource(item) {
  if (item.poster) return { type: 'image', src: item.poster };
  if (item.src) {
    if (item.kind === 'photo') return { type: 'image', src: item.src };
    if (/\.(gif|png|jpe?g|webp)(\?|$)/i.test(item.src)) {
      return { type: 'image', src: item.src };
    }
    return { type: 'video', src: item.src };
  }
  return null;
}

function wallMissing(kind) {
  return h(
    'div',
    { class: 'wall-missing' },
    icon(kind === 'photo' ? 'image' : 'film'),
    h('strong', { text: '文件缺失' }),
    h('small', { text: mediaKindLabel(kind) })
  );
}

function renderMediaTile(item) {
  const cell = h('div', {
    class: 'wall-cell',
    role: 'listitem',
    dataset: { kind: item.kind },
  });

  const source = item.missing ? null : mediaItemSource(item);
  if (!source) {
    cell.classList.add('is-missing');
    cell.append(wallMissing(item.kind));
    return cell;
  }

  const button = h('button', {
    type: 'button',
    class: 'media-tile',
    dataset: { mediaKey: item.media_key },
    'aria-label': `查看 @${item.handle || 'local'} 的${mediaKindLabel(item.kind)}${
      item.media_count > 1 ? ` ${item.media_index + 1}/${item.media_count}` : ''
    }`,
    title: `${mediaKindLabel(item.kind)} · @${item.handle || 'local'}`,
  });

  let node;
  if (source.type === 'video') {
    node = h('video', {
      src: source.src,
      preload: 'metadata',
      muted: true,
      playsinline: true,
      class: 'wall-image',
    });
    node.setAttribute('aria-hidden', 'true');
  } else {
    node = h('img', {
      src: source.src,
      alt: '',
      loading: 'lazy',
      decoding: 'async',
      draggable: 'false',
      class: 'wall-image',
    });
  }

  node.addEventListener(
    'error',
    () => {
      button.remove();
      cell.classList.add('is-missing');
      cell.append(wallMissing(item.kind));
    },
    { once: true }
  );

  button.append(node);

  if (item.kind === 'video') {
    const play = h('span', { class: 'wall-play', 'aria-hidden': 'true' });
    play.append(icon('play'));
    button.append(play);
    const duration = formatDuration(item.ms);
    if (duration) {
      button.append(h('span', { class: 'wall-duration', text: duration }));
    }
  } else if (item.kind === 'animated_gif') {
    button.append(h('span', { class: 'wall-gif', text: 'GIF' }));
  }

  cell.append(button);
  return cell;
}

function openMediaItem(item, trigger) {
  if (!item) return;
  const card = {
    id: item.tweet_id,
    handle: item.handle,
    name: item.name,
    text: item.text,
    created: item.created,
    media: item.tweet_media,
    reply_count: item.reply_count,
    retweet_count: item.retweet_count,
    quote_count: item.quote_count,
    like_count: item.like_count,
    bookmark_count: item.bookmark_count,
    view_count: item.view_count,
  };
  openLightbox(card, item.media_index, trigger);
}

function setupMediaObserver() {
  if (!('IntersectionObserver' in window)) {
    loadMediaMore();
    return;
  }
  if (state.media.observer) state.media.observer.disconnect();
  state.media.observer = new IntersectionObserver(
    (entries) => {
      if (entries.some((entry) => entry.isIntersecting)) loadMediaMore();
    },
    { rootMargin: '800px 0px' }
  );
  state.media.observer.observe(els.mediaSentinel);
}

async function loadMediaMore() {
  if (state.media.loading || state.media.done) return;
  state.media.loading = true;
  const generation = state.media.generation;
  els.mediaSentinel.replaceChildren(
    h('div', { class: 'load-more', text: '正在读取更多媒体…' })
  );

  try {
    const page = await getJSON(mediaUrl(state.media.offset));
    if (generation !== state.media.generation) return;

    state.media.total = Number(page.total) || state.media.total;
    appendMediaItems(page.items || []);
    state.media.offset += (page.items || []).length;
    state.media.loaded += (page.items || []).length;

    if (
      state.media.loaded >= state.media.total ||
      (page.items || []).length === 0
    ) {
      finishMedia();
    } else {
      els.mediaSentinel.replaceChildren();
      // sentinel 可能仍落在 rootMargin 里；重挂一次让 observer
      // 再发一次初始回调，否则会在 12/12 处停住。
      setupMediaObserver();
    }
    updateMediaHead();
  } catch (error) {
    if (generation === state.media.generation) {
      renderMediaMoreError(error);
    }
  } finally {
    if (generation === state.media.generation) state.media.loading = false;
  }
}

function finishMedia() {
  state.media.done = true;
  if (state.media.observer) {
    state.media.observer.disconnect();
    state.media.observer = null;
  }
  els.mediaView.setAttribute('aria-busy', 'false');
  els.mediaSentinel.replaceChildren(
    h(
      'div',
      { class: 'feed-end' },
      h('span', { text: `已显示全部 ${num(state.media.total)} 个媒体` })
    )
  );
}

function renderMediaMoreError(error) {
  const retry = h('button', {
    type: 'button',
    class: 'state-action',
    text: '重试',
    onclick: () => {
      els.mediaSentinel.replaceChildren();
      loadMediaMore();
    },
  });
  els.mediaSentinel.replaceChildren(
    h(
      'div',
      { class: 'load-more', role: 'alert' },
      h('span', { text: `读取更多媒体失败：${error.message}` }),
      h('br'),
      retry
    )
  );
}

function mediaEmptyKind() {
  if (state.q || state.author) return 'search';
  if (state.media.filter !== 'all') return 'filter';
  return 'all';
}

function renderMediaEmpty(kind) {
  els.mediaSkeleton.hidden = true;
  els.mediaGrid.hidden = true;
  els.mediaSentinel.replaceChildren();
  els.mediaView.setAttribute('aria-busy', 'false');

  let kicker = '媒体为空';
  let title = '媒体墙还是空的';
  let body = '这个归档里还没有可显示的媒体。';
  let action = h('a', { class: 'state-action', href: '/', text: '回到时间线' });

  if (kind === 'search') {
    kicker = '没有结果';
    title = '没有找到匹配的媒体';
    body = '换个关键词，或者清除搜索和筛选再看。';
    action = h('button', {
      type: 'button',
      class: 'state-action',
      text: '清除筛选',
      onclick: clearMediaFilters,
    });
  } else if (kind === 'filter') {
    kicker = '筛选为空';
    title = '这个筛选下没有媒体';
    body = '换一个媒体类型，或者回到全部媒体。';
    action = h('button', {
      type: 'button',
      class: 'state-action',
      text: '查看全部',
      onclick: clearMediaFilters,
    });
  }

  els.mediaState.hidden = false;
  els.mediaState.replaceChildren(stateBox(kicker, title, body, action));
}

function clearMediaFilters() {
  state.q = '';
  state.author = '';
  state.media.filter = 'all';
  state.sort = 'created';
  state.since = '';
  state.until = '';
  state.minLikes = '';
  state.minViews = '';
  updateMediaTabs();
  updateAdvancedUi();
  updateAuthorContext();
  updateSearchInputs('');
  syncUrl();
  reloadMedia();
}

function renderMediaError(error) {
  els.mediaSkeleton.hidden = true;
  els.mediaGrid.hidden = true;
  els.mediaSentinel.replaceChildren();
  els.mediaView.setAttribute('aria-busy', 'false');
  const retry = h('button', {
    type: 'button',
    class: 'state-action',
    text: '重新加载',
    onclick: reloadMedia,
  });
  els.mediaState.hidden = false;
  els.mediaState.replaceChildren(
    stateBox(
      '读取失败',
      '媒体墙没有读出来',
      `本地服务没有返回可用的媒体。${error && error.message ? `（${error.message}）` : ''}`,
      retry
    )
  );
  els.headMeta.textContent = '读取失败';
}

function renderWallBar(photo, video, gif) {
  els.mediaSideBar.replaceChildren();
  const total = photo + video + gif;
  if (total <= 0) return;
  for (const [count, className] of [
    [photo, 'bar-photo'],
    [video, 'bar-video'],
    [gif, 'bar-gif'],
  ]) {
    if (count <= 0) continue;
    const segment = h('span', { class: className });
    segment.style.flex = `${count} 1 0`;
    els.mediaSideBar.append(segment);
  }
}

function renderMediaSide(meta, page) {
  const counts = (meta && meta.counts) || {};
  const types =
    (meta && (meta.media_types_in_timeline || meta.media_types)) || {};
  const total =
    counts.media_in_timeline != null
      ? Number(counts.media_in_timeline) || 0
      : counts.media_rows != null
        ? Number(counts.media_rows) || 0
        : Number(page && page.total) || 0;

  els.mediaBigStat.classList.remove('is-loading');
  els.mediaTotal.textContent = num(total);

  let oldest = meta && meta.oldest ? meta.oldest : null;
  let newest = meta && meta.newest ? meta.newest : null;
  if ((!oldest || !newest) && page && page.items && page.items.length) {
    newest = newest || page.items[0].created;
    oldest = oldest || page.items[page.items.length - 1].created;
  }
  if (oldest && newest) {
    els.mediaRange.textContent = `${monthStamp(oldest)} — ${monthStamp(newest)}`;
    els.mediaOldest.textContent = dayStamp(oldest);
    els.mediaNewest.textContent = dayStamp(newest);
  } else {
    els.mediaRange.textContent = '暂无时间信息';
    els.mediaOldest.textContent = '—';
    els.mediaNewest.textContent = '—';
  }
  els.mediaGenerated.textContent =
    meta && meta.generated_at ? dayStamp(meta.generated_at) : '—';

  const photo = Number(types.photo) || 0;
  const video = Number(types.video) || 0;
  const gif = Number(types.animated_gif) || 0;
  els.mediaCountPhoto.textContent = types.photo == null ? '—' : num(photo);
  els.mediaCountVideo.textContent = types.video == null ? '—' : num(video);
  els.mediaCountGif.textContent =
    types.animated_gif == null ? '—' : num(gif);
  renderWallBar(photo, video, gif);
  const missing = counts.media_files_missing;
  els.mediaMissing.textContent = missing == null ? '—' : num(missing);
}

function renderMediaSideLoading() {
  els.mediaBigStat.classList.add('is-loading');
  els.mediaTotal.textContent = '';
  els.mediaRange.textContent = '读取媒体信息…';
  els.mediaCountPhoto.textContent = '—';
  els.mediaCountVideo.textContent = '—';
  els.mediaCountGif.textContent = '—';
  els.mediaMissing.textContent = '—';
  els.mediaOldest.textContent = '—';
  els.mediaNewest.textContent = '—';
  els.mediaGenerated.textContent = '—';
  els.mediaSideBar.replaceChildren();
}

function renderMediaSideEmpty() {
  els.mediaBigStat.classList.remove('is-loading');
  els.mediaTotal.textContent = '0';
  els.mediaRange.textContent = '暂无媒体';
  els.mediaSideBar.replaceChildren();
  els.mediaCountPhoto.textContent = '0';
  els.mediaCountVideo.textContent = '0';
  els.mediaCountGif.textContent = '0';
  els.mediaMissing.textContent = '0';
  els.mediaOldest.textContent = '—';
  els.mediaNewest.textContent = '—';
  els.mediaGenerated.textContent = '—';
}

function updateMediaHead() {
  const total = state.media.total;
  const unit =
    state.media.filter === 'photo'
      ? '张图片'
      : state.media.filter === 'video'
        ? '个视频'
        : state.media.filter === 'gif'
          ? '张动图'
          : '个媒体';

  let range = '';
  const oldest = state.meta && state.meta.oldest;
  const newest = state.meta && state.meta.newest;
  if (oldest && newest) {
    const first = yearOf(oldest);
    const last = yearOf(newest);
    range = first === last ? `${first}` : `${first} — ${last}`;
  }

  if (state.author) {
    els.headMeta.textContent = `@${state.author} · ${num(total)} 个媒体`;
    return;
  }
  if (state.q) {
    els.headMeta.textContent = `搜索 “${cut(state.q, 22)}” · ${num(total)} 个媒体`;
    return;
  }
  els.headMeta.textContent = `${num(total)} ${unit}${range ? ` · ${range}` : ''}`;
}

function updateMediaTabs() {
  if (!els.mediaTabs) return;
  els.mediaTabs.querySelectorAll('[data-media-filter]').forEach((tab) => {
    const active = tab.dataset.mediaFilter === state.media.filter;
    tab.classList.toggle('is-active', active);
    tab.setAttribute('aria-pressed', active ? 'true' : 'false');
  });
}

function reloadCurrent() {
  if (VIEW === 'media') reloadMedia();
  else reloadFeed();
}

function clearFilters() {
  if (VIEW === 'media') {
    clearMediaFilters();
    return;
  }
  state.q = '';
  if (VIEW !== 'author') state.author = '';
  state.mediaType = 'all';
  state.readState = 'all';
  if (VIEW !== 'favorites') state.source = 'all';
  state.account = '';
  state.sort = 'created';
  state.since = '';
  state.until = '';
  state.minLikes = '';
  state.minViews = '';
  updateFilterUi();
  updateAdvancedUi();
  updateAccountUi();
  updateAuthorContext();
  updateSearchInputs('');
  syncUrl();
  reloadFeed();
}

function setAuthor(handle) {
  if (!handle) return;
  window.location.assign(`/author/${encodeURIComponent(handle)}`);
}

function clearAuthor() {
  if (!state.author) return;
  state.author = '';
  updateAuthorContext();
  updateSearchInputs(state.q || '');
  syncUrl();
  reloadCurrent();
}

/* =========================================================
   推文渲染
   ========================================================= */

function renderTweet(card, index, setSize) {
  const article = h('article', {
    class: `tweet${card.deleted ? ' is-deleted' : ''}`,
    id: `tweet-${card.id}`,
    role: 'article',
    tabindex: '-1',
    'aria-posinset': String(index),
    'aria-setsize': String(setSize || state.total || index),
    dataset: { tweetId: card.id, seen: card.seen ? '1' : '0' },
  });

  const grid = h('div', { class: 'tweet-grid' });
  const avatar = card.handle
    ? h('button', {
        type: 'button',
        class: 'avatar',
        dataset: { author: card.handle },
        title: `查看 @${card.handle} 的作者页`,
        'aria-label': `查看 @${card.handle} 的作者页`,
      })
    : h('span', { class: 'avatar', 'aria-hidden': 'true' });
  fillAvatar(avatar, card.avatar, card.name, card.handle);

  const main = h('div', { class: 'tweet-main' });
  main.append(renderByline(card));

  const rawText = (card.text || '').trim();
  let usedUrls = new Set();
  if (rawText) {
    const textEl = h('div', { class: 'tweet-text' });
    usedUrls = renderRichText(textEl, rawText, card.urls);
    main.append(textEl);
  }

  const extra = renderExtraLinks(card.urls, usedUrls);
  if (extra) main.append(extra);
  const previews = renderLinkPreviews(card.urls);
  if (previews) main.append(previews);

  const media = renderMedia(card);
  if (media) main.append(media);

  main.append(renderActions(card));
  const localNote = noteBlock(card);
  if (localNote) main.append(localNote);
  grid.append(avatar, main);
  article.append(grid);
  article.append(renderTweetMenu(card));
  return article;
}

function renderByline(card) {
  const main = h('div', { class: 'byline-main' });
  main.append(h('span', { class: 'display-name', text: card.name || card.handle || '未知作者' }));
  if (card.handle) main.append(h('span', { class: 'handle', text: `@${card.handle}` }));
  main.append(h('span', { class: 'sep', text: '·', 'aria-hidden': 'true' }));

  const time = h(
    'a',
    {
      class: 'time',
      href: `/tweet/${encodeURIComponent(card.id)}`,
      'aria-label': '查看推文详情',
    },
    h('time', {
      datetime: card.created || '',
      text: formatTweetTime(card.created),
    })
  );
  if (card.created && !Number.isNaN(new Date(card.created).getTime())) {
    time.title = `${new Intl.DateTimeFormat('zh-CN', {
      year: 'numeric',
      month: 'long',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    }).format(new Date(card.created))} · 查看详情`;
  }
  main.append(time);

  if (card.deleted) {
    main.append(h('span', { class: 'sep', text: '·', 'aria-hidden': 'true' }));
    main.append(h('span', { class: 'deleted-mark', text: '已删除' }));
  }

  // 原站来源徽标：只读，来自归档时 X 的收藏/喜欢集合，和「本地点赞/收藏」区分开。
  const collections = Array.isArray(card.collections) ? card.collections.map(String) : [];
  if (collections.includes('bookmark')) {
    main.append(h('span', { class: 'sep', text: '·', 'aria-hidden': 'true' }));
    main.append(originBadge('原站书签', 'bookmark'));
  }
  if (collections.includes('like')) {
    main.append(h('span', { class: 'sep', text: '·', 'aria-hidden': 'true' }));
    main.append(originBadge('原站点赞', 'like'));
  }

  const more = h('button', {
    type: 'button',
    class: 'more-btn',
    'aria-label': '更多操作',
    'aria-haspopup': 'menu',
    'aria-expanded': 'false',
    dataset: { more: card.id },
  });
  more.append(icon('more'));
  return h('header', { class: 'byline' }, main, more);
}

function originBadge(label, kind) {
  const title =
    kind === 'bookmark'
      ? '归档时在 X 书签里（只读）'
      : '归档时在 X 喜欢里（只读）';
  const badge = h('span', {
    class: `origin-badge is-${kind}`,
    title,
    'aria-label': title,
  });
  badge.append(
    icon(kind === 'bookmark' ? 'bookmark' : 'like', 'icon origin-badge-icon'),
    h('span', { text: label })
  );
  return badge;
}

function makeAction(card, action, label, iconName, options = {}) {
  const button = h('button', {
    type: 'button',
    class: `action ${action}${options.active ? ' is-active' : ''}`,
    'aria-label': label,
    title: options.title || label,
    dataset: { action, tweetId: card.id },
  });
  if (options.pressed !== undefined) {
    button.setAttribute('aria-pressed', options.pressed ? 'true' : 'false');
  }
  if (options.dialog) button.setAttribute('aria-haspopup', 'dialog');
  if (options.keys) button.setAttribute('aria-keyshortcuts', options.keys);
  button.append(icon(iconName));
  return button;
}

function withArchiveCount(button, count, unit, detail) {
  const value = toCount(count);
  if (!value) return button;
  const label = detail || `归档时 ${num(value)} ${unit}`;
  const title = button.getAttribute('title') || button.getAttribute('aria-label');
  button.setAttribute('aria-label', `${button.getAttribute('aria-label')}，${label}`);
  if (title) button.setAttribute('title', `${title} · ${label}`);
  return h(
    'div',
    { class: 'action-slot' },
    button,
    h('span', {
      class: 'action-count',
      'aria-hidden': 'true',
      text: compactCount(value),
    })
  );
}

function archiveViewSlot(count) {
  const value = toCount(count);
  if (!value) return null;
  const label = `归档时 ${num(value)} 次查看`;
  const slot = h('span', {
    class: 'action-slot action-view',
    role: 'img',
    'aria-label': label,
    title: label,
  });
  slot.append(
    h('span', { class: 'action-icon view-icon', 'aria-hidden': 'true' }, icon('stats')),
    h('span', {
      class: 'action-count',
      'aria-hidden': 'true',
      text: compactCount(value),
    })
  );
  return slot;
}

function renderActions(card) {
  const group = h('div', { class: 'action-group' });
  const localNote = noteFor(card.id);
  const marks = markFor(card.id);
  const counts = {
    reply: toCount(card.reply_count),
    repost: toCount(card.retweet_count),
    quote: toCount(card.quote_count),
    like: toCount(card.like_count),
    bookmark: toCount(card.bookmark_count),
    views: toCount(card.view_count),
  };

  const replyButton = makeAction(
    card,
    'reply',
    localNote ? '编辑本地备注' : '添加本地备注',
    'reply',
    {
      active: Boolean(localNote),
      dialog: true,
      keys: 'R',
      title: '只保存在这台浏览器',
    }
  );
  replyButton.setAttribute('aria-expanded', 'false');
  group.append(withArchiveCount(replyButton, counts.reply, '条回复'));

  const repostButton = makeAction(
    card,
    'repost',
    marks.reposted ? '取消本地转发标记' : '本地转发标记',
    'repost',
    {
      active: Boolean(marks.reposted),
      pressed: Boolean(marks.reposted),
      keys: 'T',
      title: '本地标记，不会发到 X',
    }
  );
  const repostTotal = counts.repost + counts.quote;
  const repostDetail = counts.quote
    ? `归档时 ${num(counts.repost)} 次转推、${num(counts.quote)} 次引用`
    : null;
  group.append(withArchiveCount(repostButton, repostTotal, '次转推', repostDetail));

  const likeActive = localLikeActive(card);
  const likeButton = makeAction(
    card,
    'like',
    likeActive ? '取消本地点赞' : '本地点赞',
    likeActive ? 'like-fill' : 'like',
    {
      active: likeActive,
      pressed: likeActive,
      keys: 'L',
      title: '本地点赞（仅本机，不会发到 X）',
    }
  );
  group.append(withArchiveCount(likeButton, counts.like, '次喜欢'));

  const favorited =
    card.favorited !== undefined && card.favorited !== null
      ? Boolean(card.favorited)
      : Boolean(card.local_fav);
  const bookmark = makeAction(
    card,
    'bookmark',
    favorited ? '取消本地收藏' : '本地收藏',
    favorited ? 'bookmark-fill' : 'bookmark',
    {
      active: favorited,
      pressed: favorited,
      keys: 'B',
      title: '本地收藏（仅本机，不会发到 X）',
    }
  );
  bookmark.dataset.bookmark = '1';
  if (!card.related) {
    group.append(withArchiveCount(bookmark, counts.bookmark, '次收藏'));
  }

  const views = archiveViewSlot(counts.views);
  if (views) group.append(views);

  const share = makeAction(card, 'share', '复制原帖链接', 'share', {
    keys: 'S',
    title: '复制原帖链接',
  });

  return h(
    'footer',
    { class: 'actions', role: 'group', 'aria-label': '推文操作' },
    group,
    share
  );
}

function noteBlock(card) {
  const localNote = noteFor(card.id);
  if (!localNote) return null;

  const head = h('div', { class: 'tweet-note-head' });
  head.append(h('strong', { text: '我的备注' }));
  head.append(
    h('time', {
      datetime: localNote.updated_at || '',
      text: relativeTime(localNote.updated_at),
    })
  );
  head.append(
    h('button', {
      type: 'button',
      class: 'tweet-note-edit',
      text: '编辑',
      dataset: { noteEdit: card.id },
      'aria-label': '编辑本地备注',
      'aria-haspopup': 'dialog',
    })
  );

  return h(
    'div',
    { class: 'tweet-note' },
    head,
    h('p', { text: localNote.text })
  );
}

function renderTweetMenu(card) {
  const menu = h('div', {
    class: 'tweet-menu',
    role: 'menu',
    'aria-label': '更多推文操作',
    hidden: true,
    dataset: { menuFor: card.id },
  });

  menu.append(
    menuItem(card, 'detail', '查看详情', 'external'),
    menuItem(card, 'copy-text', '复制文本', 'copy'),
    menuItem(card, 'copy-link', '复制原帖链接', 'link')
  );
  if (card.handle) {
    menu.append(menuItem(card, 'author', `查看 @${card.handle} 的作者页`, 'user'));
  }
  menu.append(
    menuItem(
      card,
      'note',
      noteFor(card.id) ? '编辑本地备注' : '添加本地备注',
      'reply'
    )
  );
  return menu;
}

function menuItem(card, action, label, iconName) {
  const button = h('button', {
    type: 'button',
    role: 'menuitem',
    tabindex: '-1',
    dataset: { menuAction: action, tweetId: card.id },
  });
  button.append(icon(iconName), h('span', { text: label }));
  return button;
}

function renderRichText(container, text, urls) {
  const candidates = [];
  const seen = new Set();

  for (const url of urls || []) {
    if (!url || !url.u) continue;
    for (const label of [url.d, url.u]) {
      if (!label || !text.includes(label)) continue;
      const key = `${label}\u0000${url.u}`;
      if (seen.has(key)) continue;
      seen.add(key);
      candidates.push({ label, href: url.u, display: url.d || label });
    }
  }

  candidates.sort((a, b) => b.label.length - a.label.length);
  const alternatives = candidates.map((item) => escapeRegExp(item.label));
  alternatives.push('https?:\\/\\/[^\\s]+');
  // 正文 @提及：Twitter handle 只允许字母数字下划线，最长 15；
  // 放在 URL 后面，URL 里含 @ 的部分会先被 URL 分支整体吃掉。
  alternatives.push('@[A-Za-z0-9_]{1,15}');

  let pattern;
  try {
    pattern = new RegExp(`(${alternatives.join('|')})`, 'g');
  } catch (_) {
    pattern = /(https?:\/\/[^\s]+)/g;
  }

  const usedUrls = new Set();
  let lastIndex = 0;
  let match;

  while ((match = pattern.exec(text)) !== null) {
    if (match.index > lastIndex) {
      container.append(textNode(text.slice(lastIndex, match.index)));
    }

    let token = match[0];
    let trailing = '';
    if (/^https?:\/\//i.test(token)) {
      const trimmed = token.replace(/[.,;:!?，。；：！？、)\]）】]+$/, '');
      trailing = token.slice(trimmed.length);
      token = trimmed;
    }

    if (token.startsWith('@')) {
      const before = match.index > 0 ? text[match.index - 1] : '';
      // 邮箱 / 已在上文 URL 里的 @ 不当作提及
      if (before && /[A-Za-z0-9_@./]/.test(before)) {
        container.append(textNode(match[0]));
      } else {
        container.append(mentionLink(token));
      }
      lastIndex = match.index + match[0].length;
      continue;
    }

    const known = candidates.find((item) => item.label === token);
    const href = known ? known.href : token;
    const safeHref = /^https?:\/\//i.test(href) ? href : '';
    const label = known ? known.display || token : prettyUrl(token);

    if (safeHref) {
      const anchor = h('a', {
        href: safeHref,
        target: '_blank',
        rel: 'noopener noreferrer',
        text: label,
      });
      container.append(anchor);
      usedUrls.add(safeHref);
    } else {
      container.append(textNode(token));
    }

    if (trailing) container.append(textNode(trailing));
    lastIndex = match.index + match[0].length;
  }

  if (lastIndex < text.length) {
    container.append(textNode(text.slice(lastIndex)));
  }
  return usedUrls;
}

function mentionLink(token) {
  const handle = token.replace(/^@/, '');
  const known = state.knownHandles.has(handle.toLowerCase());
  const anchor = h('a', {
    class: 'mention',
    href: mentionHref(handle),
    dataset: { mention: handle },
    title: known ? `查看 @${handle} 的作者页` : `在本地归档里搜索 @${handle}`,
    text: token,
  });
  return anchor;
}

function renderLinkPreviews(urls) {
  if (!urls || !urls.length) return null;
  const wrap = h('div', { class: 'link-previews' });
  let shown = 0;
  for (const url of urls) {
    if (!url) continue;
    const title = (url.title || '').trim();
    const site = (url.site || url.host || '').trim();
    const desc = (url.desc || '').trim();
    if (!title && !site && !desc) continue;
    const href = url.final || url.canonical || url.u;
    if (!href || !/^https?:\/\//i.test(href)) continue;
    const preview = h('a', {
      class: 'link-preview',
      href,
      target: '_blank',
      rel: 'noopener noreferrer',
    });
    preview.append(
      h('span', {
        class: 'link-preview-site',
        text: site || prettyUrl(href),
      })
    );
    if (title) preview.append(h('strong', { text: cut(title, 120) }));
    if (desc) preview.append(h('p', { text: cut(desc, 180) }));
    wrap.append(preview);
    shown += 1;
    if (shown >= 2) break;
  }
  return wrap.childElementCount ? wrap : null;
}

function renderExtraLinks(urls, usedUrls) {
  if (!urls || !urls.length) return null;
  const list = h('div', { class: 'link-list' });
  const seen = new Set(usedUrls);

  for (const url of urls) {
    if (!url || !url.u || !/^https?:\/\//i.test(url.u) || seen.has(url.u)) continue;
    seen.add(url.u);
    const label = h('span', { text: prettyUrl(url.u) });
    list.append(
      h(
        'a',
        {
          class: 'link-out',
          href: url.u,
          target: '_blank',
          rel: 'noopener noreferrer',
        },
        icon('link'),
        label,
        icon('external')
      )
    );
  }
  return list.childElementCount ? list : null;
}

/* =========================================================
   媒体
   ========================================================= */

function renderMedia(card) {
  const all = (card.media || []).filter(Boolean);
  if (!all.length) return null;

  const shown = all.slice(0, 4);
  const grid = h('div', {
    class: 'media-grid',
    dataset: { count: String(shown.length) },
    'aria-label':
      all.length > 4 ? `共 ${all.length} 个媒体，这里显示前 4 个` : '媒体',
  });

  shown.forEach((media, index) => {
    const cell = renderMediaCell(media, card, index);
    if (all.length > 4 && index === 3) {
      cell.append(h('span', { class: 'media-more', text: `+${all.length - 4}` }));
    }
    grid.append(cell);
  });

  return h('div', { class: 'tweet-media' }, grid);
}

function renderMediaCell(media, card, index) {
  const cell = h('div', {
    class: 'media-cell',
    dataset: { kind: media.t || 'photo' },
  });

  if (media.t === 'photo') {
    if (media.src && !media.missing) {
      cell.append(createImage(media.src, mediaAlt(card, media, index), 'photo', cell));
      addMediaOpen(cell, card, index, 'photo');
    } else {
      cell.classList.add('is-missing');
      cell.append(missingMedia('photo'));
    }
    return cell;
  }

  if (media.t === 'animated_gif') {
    cell.classList.add('is-gif');

    if (media.src && !media.missing) {
      if (/\.(mp4|webm|m4v)(\?|$)/i.test(media.src)) {
        const video = h('video', {
          src: media.src,
          muted: true,
          loop: true,
          autoplay: true,
          playsinline: true,
        });
        video.setAttribute('aria-label', mediaAlt(card, media, index));
        video.addEventListener(
          'error',
          () => {
            cell.classList.add('is-missing');
            cell.replaceChildren(missingMedia('animated_gif'));
          },
          { once: true }
        );
        cell.append(video);
        attachVideoReporter(video, card, media);
      } else {
        cell.append(createImage(media.src, mediaAlt(card, media, index), 'animated_gif', cell));
      }
      cell.append(h('span', { class: 'gif-mark', text: 'GIF' }));
      addMediaOpen(cell, card, index, 'animated_gif');
      return cell;
    }

    if (media.poster) {
      cell.append(createImage(media.poster, mediaAlt(card, media, index), 'animated_gif', cell));
      cell.append(h('span', { class: 'gif-mark', text: 'GIF' }));
      addMediaOpen(cell, card, index, 'animated_gif');
      return cell;
    }

    cell.classList.add('is-missing');
    cell.append(missingMedia('animated_gif'));
    return cell;
  }

  if (media.t === 'video') {
    cell.classList.add('is-video');

    if (media.src && !media.missing) {
      const video = h('video', {
        src: media.src,
        preload: 'none',
        muted: true,
        playsinline: true,
      });
      if (media.poster) video.poster = media.poster;
      video.setAttribute('aria-label', mediaAlt(card, media, index));
      video.addEventListener(
        'error',
        () => {
          cell.classList.add('is-missing');
          cell.replaceChildren(missingMedia('video'));
        },
        { once: true }
      );

      const play = createPlayButton();
      play.addEventListener('click', (event) => {
        event.stopPropagation();
        if (video.paused) video.play().catch(() => {});
        else video.pause();
      });

      video.addEventListener('play', () => {
        cell.classList.add('is-playing');
        setPlayButton(play, 'pause', '暂停视频');
      });
      video.addEventListener('pause', () => {
        cell.classList.remove('is-playing');
        setPlayButton(play, 'play', '播放视频');
      });
      video.addEventListener('ended', () => {
        cell.classList.remove('is-playing');
        setPlayButton(play, 'play', '播放视频');
      });

      cell.append(video, play);
      addMediaOpen(cell, card, index, 'video');
      appendDuration(cell, media.ms);
      applyResume(video, media);
      attachVideoReporter(video, card, media);
      setupAutoplay(video, cell);
      return cell;
    }

    if (media.poster) {
      cell.append(createImage(media.poster, mediaAlt(card, media, index), 'video', cell));
      const play = createPlayButton();
      play.addEventListener('click', () => {
        cell.replaceChildren(missingMedia('video'));
      });
      cell.append(play);
      addMediaOpen(cell, card, index, 'video');
      appendDuration(cell, media.ms);
      return cell;
    }

    cell.classList.add('is-missing');
    cell.append(missingMedia('video'));
    return cell;
  }

  cell.classList.add('is-missing');
  cell.append(missingMedia('photo'));
  return cell;
}

function addMediaOpen(cell, card, index, kind) {
  const labels = {
    photo: '查看图片',
    video: '查看视频',
    animated_gif: '查看动图',
  };
  const button = h('button', {
    type: 'button',
    class: 'media-open',
    'aria-label': labels[kind] || '查看媒体',
    dataset: { tweetId: card.id, mediaIndex: String(index) },
  });
  cell.append(button);
}

function createImage(src, alt, kind, cell) {
  const image = h('img', {
    src,
    alt,
    loading: 'lazy',
    decoding: 'async',
    draggable: 'false',
  });
  image.addEventListener(
    'error',
    () => {
      cell.classList.add('is-missing');
      cell.replaceChildren(missingMedia(kind));
    },
    { once: true }
  );
  return image;
}

function createPlayButton() {
  const button = h('button', {
    type: 'button',
    class: 'play',
    'aria-label': '播放视频',
  });
  const disc = h('span', { class: 'play-disc' });
  disc.append(icon('play'));
  button.append(disc);
  return button;
}

function setPlayButton(button, name, label) {
  const use = button.querySelector('use');
  if (use) use.setAttribute('href', `#i-${name}`);
  button.setAttribute('aria-label', label);
}

function appendDuration(cell, ms) {
  const duration = formatDuration(ms);
  if (duration) cell.append(h('span', { class: 'duration', text: duration }));
}

function missingMedia(kind) {
  const labels = {
    photo: '图片文件缺失',
    video: '视频文件缺失',
    animated_gif: '动图文件缺失',
  };
  return h(
    'div',
    { class: 'media-missing' },
    icon(kind === 'photo' ? 'image' : 'film'),
    h('strong', { text: labels[kind] || '媒体文件缺失' }),
    h('small', { text: '原始文件未随归档保存' })
  );
}

function mediaAlt(card, media, index) {
  const who = card.name || card.handle || '推文';
  const kind =
    media.t === 'video' ? '视频' : media.t === 'animated_gif' ? '动图' : '图片';
  const base = (card.text || '').replace(/\s+/g, ' ').trim();
  if (base) return `${who}的${kind}：${base.slice(0, 42)}`;
  return `${who}的${kind} ${index + 1}`;
}

/* =========================================================
   视频上报 + 自动播放
   ========================================================= */

function applyResume(video, media) {
  const pos = Math.max(0, Math.trunc(Number(media && media.last_pos_ms) || 0));
  if (pos < 2000) return;
  const seek = () => {
    const duration =
      Number.isFinite(video.duration) && video.duration > 0
        ? video.duration * 1000
        : Number(media.ms) || 0;
    if (duration && pos >= duration - 1000) return;
    try {
      video.currentTime = pos / 1000;
    } catch (error) {
      /* 浏览器还没拿到 seekable range 时忽略，下一次加载会重试 */
    }
  };
  if (video.readyState >= 1) seek();
  else video.addEventListener('loadedmetadata', seek, { once: true });
}

function attachVideoReporter(video, card, media) {
  let instanceId = null;
  let playing = false;
  let lastTick = 0;
  let watched = 0;
  let timer = null;

  function startTimer() {
    clearInterval(timer);
    timer = setInterval(() => {
      if (!playing) return;
      accrue();
      if (watched >= 500) {
        send('progress', watched);
        watched = 0;
      }
    }, 5000);
  }

  function accrue() {
    // 暂停事件触发时 video.paused 已经是 true，不能再用它当守卫；
    // playing 是本段播放是否还活着的唯一依据，否则暂停前那几秒会丢。
    if (!playing) return;
    const now = performance.now();
    watched += Math.max(0, now - lastTick);
    lastTick = now;
  }

  function build(action, watchedMs) {
    return {
      media_key: media.key,
      tweet_id: card.id,
      instance_id: instanceId,
      action,
      watched_ms: Math.round(watchedMs || 0),
      pos_ms: Math.round((video.currentTime || 0) * 1000),
      duration_ms:
        media.ms || (Number.isFinite(video.duration) ? Math.round(video.duration * 1000) : null),
    };
  }

  function send(action, watchedMs, keepalive) {
    if (!media.key || !instanceId) return;
    const payload = build(action, watchedMs);
    if (keepalive) {
      try {
        fetch('/api/state/play', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
          keepalive: true,
        }).catch(() => {});
      } catch (_) {
        /* 忽略 */
      }
    } else {
      postJSON('/api/state/play', payload).catch(() => {});
    }
  }

  video.addEventListener('play', () => {
    if (!instanceId) instanceId = newId();
    if (!playing) {
      playing = true;
      watched = 0;
      lastTick = performance.now();
      send('start', 0);
      startTimer();
    }
    state.activeVideo = video;
    document.querySelectorAll('video').forEach((other) => {
      if (other !== video && !other.paused) other.pause();
    });
  });

  video.addEventListener('pause', () => {
    if (!playing) return;
    accrue();
    if (watched > 0) send('progress', watched);
    watched = 0;
    send('pos', 0);
    playing = false;
    clearInterval(timer);
    timer = null;
    if (state.activeVideo === video) state.activeVideo = null;
  });

  video.addEventListener('ended', () => {
    accrue();
    send('end', watched);
    watched = 0;
    playing = false;
    clearInterval(timer);
    timer = null;
    instanceId = null;
    if (state.activeVideo === video) state.activeVideo = null;
  });

  video.addEventListener('seeking', () => {
    if (!playing) return;
    accrue();
    if (watched > 0) {
      send('progress', watched);
      watched = 0;
    }
    send('pos', 0);
    lastTick = performance.now();
  });

  state.videoFlush.push(() => {
    if (!playing) return;
    accrue();
    if (watched > 0) send('pos', watched, true);
    watched = 0;
    playing = false;
    clearInterval(timer);
  });
}

function setupAutoplay(video, cell) {
  if (!('IntersectionObserver' in window)) return;

  const entry = { video, cell, observer: null, observed: false };
  entry.observer = new IntersectionObserver(
    (entries) => {
      if (!state.autoplay || state.reduceMotion || prefersReducedMotion()) return;
      for (const item of entries) {
        if (item.intersectionRatio >= 0.6) {
          video.play().catch(() => {});
        } else if (item.intersectionRatio < 0.2) {
          video.pause();
        }
      }
    },
    { threshold: [0, 0.2, 0.6, 1] }
  );

  state.videoEntries.push(entry);
  if (state.autoplay && !state.reduceMotion && !prefersReducedMotion()) {
    entry.observer.observe(cell);
    entry.observed = true;
  }
}

function refreshAutoplay() {
  const enabled = state.autoplay && !state.reduceMotion && !prefersReducedMotion();
  for (const entry of state.videoEntries) {
    if (enabled) {
      if (!entry.observed) {
        entry.observer.observe(entry.cell);
        entry.observed = true;
      }
    } else {
      entry.video.pause();
      entry.observer.disconnect();
      entry.observed = false;
    }
  }
}

function resetVideos() {
  document.querySelectorAll('video').forEach((video) => video.pause());
  for (const entry of state.videoEntries) {
    entry.observer.disconnect();
  }
  state.videoEntries = [];
  state.videoFlush = [];
  state.activeVideo = null;
}

/* =========================================================
   本地收藏 / 本地点赞
   ========================================================= */

function localLikeActive(card) {
  if (!card) return false;
  if (card.local_like !== undefined && card.local_like !== null) {
    return Boolean(card.local_like);
  }
  return Boolean(markFor(card.id).liked);
}

async function toggleFavorite(tweetId, button) {
  const card = state.cards.get(tweetId);
  if (!card || button.dataset.busy === '1') return;

  const current = Boolean(card.favorited);
  const next = !current;
  button.dataset.busy = '1';
  updateBookmarkButton(button, next);
  popIcon(button);

  try {
    const data = await postJSON('/api/favorites', {
      tweet_id: tweetId,
      action: 'toggle',
    });
    card.favorited = Boolean(data.favorited);
    card.local_fav = card.favorited;
    updateBookmarkButton(button, card.favorited);
    showToast(card.favorited ? '已加入本地收藏' : '已取消本地收藏');

    if ((VIEW === 'favorites' || state.source === 'local_fav') && !card.favorited) {
      removeFavoriteCard(tweetId);
    }
  } catch (error) {
    updateBookmarkButton(button, current);
    if (error && error.status === 404) {
      showToast('收藏失败：后端 /api/favorites 未就绪（404）');
    } else {
      showToast(`收藏失败：${error.message}`);
    }
  } finally {
    delete button.dataset.busy;
  }
}

function updateBookmarkButton(button, favorited) {
  if (!button) return;
  button.classList.toggle('is-active', favorited);
  button.setAttribute('aria-pressed', favorited ? 'true' : 'false');
  button.setAttribute('aria-label', favorited ? '取消本地收藏' : '本地收藏');
  button.title = '本地收藏（仅本机，不会发到 X）';
  const use = button.querySelector('use');
  if (use) use.setAttribute('href', favorited ? '#i-bookmark-fill' : '#i-bookmark');
}

async function toggleLocalLike(card, button) {
  if (!card || (button && button.dataset.busy === '1')) return;
  const current = localLikeActive(card);
  const next = !current;
  if (button) {
    button.dataset.busy = '1';
    updateLikeButton(button, next);
    popIcon(button);
  }

  try {
    const data = await postJSON('/api/likes', {
      tweet_id: card.id,
      liked: next,
    });
    card.local_like = Boolean(data && data.liked);
    // 同步到旧的本机标记，导出 / 离线兜底仍可读到
    state.marks[card.id] = { ...markFor(card.id), liked: card.local_like };
    writeLocalStore(MARKS_KEY, state.marks);
    updateLikeButton(button, card.local_like);
    showToast(card.local_like ? '已加入本地点赞' : '已取消本地点赞');

    if (state.source === 'local_like' && !card.local_like) {
      removeFavoriteCard(card.id);
    }
  } catch (error) {
    updateLikeButton(button, current);
    if (error && error.status === 404) {
      showToast('本地点赞未保存：后端 /api/likes 未就绪（404）');
    } else {
      showToast(`本地点赞失败：${error.message}`);
    }
  } finally {
    if (button) delete button.dataset.busy;
  }
}

function updateLikeButton(button, active) {
  if (!button) return;
  button.classList.toggle('is-active', active);
  button.setAttribute('aria-pressed', active ? 'true' : 'false');
  button.setAttribute('aria-label', active ? '取消本地点赞' : '本地点赞');
  button.title = '本地点赞（仅本机，不会发到 X）';
  const use = button.querySelector('use');
  if (use) use.setAttribute('href', active ? '#i-like-fill' : '#i-like');
}

function removeFavoriteCard(tweetId) {
  const article = document.getElementById(`tweet-${tweetId}`);
  if (!article) return;
  article.classList.add('is-removing');
  window.setTimeout(
    () => {
      article.remove();
      state.total = Math.max(0, state.total - 1);
      state.cards.delete(tweetId);
      if (state.focusedId === tweetId) state.focusedId = null;
      updateHead();
      updateFeedEndLabel();
      if (state.total === 0) {
        renderEmpty(VIEW === 'favorites' ? 'favorites' : emptyKind());
      }
    },
    state.reduceMotion ? 0 : 170
  );
}

/* =========================================================
   本地标记 / 备注 / 分享
   ========================================================= */

function updateReplyButton(button, hasNote) {
  if (!button) return;
  button.classList.toggle('is-active', hasNote);
  button.setAttribute('aria-label', hasNote ? '编辑本地备注' : '添加本地备注');
  button.title = '只保存在这台浏览器';
}

function updateRepostButton(button, active) {
  if (!button) return;
  button.classList.toggle('is-active', active);
  button.setAttribute('aria-pressed', active ? 'true' : 'false');
  button.setAttribute(
    'aria-label',
    active ? '取消本地转发标记' : '本地转发标记'
  );
  button.title = '本地标记，不会发到 X';
}

function toggleLocalMark(card, kind, button) {
  if (!card) return;
  const current = markFor(card.id);
  if (kind === 'like') {
    toggleLocalLike(card, button);
    return;
  }
  const next = !current.reposted;
  state.marks[card.id] = { ...current, reposted: next };
  writeLocalStore(MARKS_KEY, state.marks);
  updateRepostButton(button, next);
  popIcon(button);
  showToast(next ? '已标记本地转发' : '已取消本地转发');
}

function tweetUrl(card) {
  const handle = String(card.handle || '').replace(/^@/, '');
  if (handle && card.id != null) {
    return `https://x.com/${encodeURIComponent(handle)}/status/${encodeURIComponent(card.id)}`;
  }
  return `${window.location.origin}/?tweet=${encodeURIComponent(card.id || '')}`;
}

async function copyText(value, successMessage) {
  const text = String(value || '');
  if (!text) {
    showToast('没有可复制的内容');
    return;
  }

  if (navigator.clipboard && window.isSecureContext) {
    try {
      await navigator.clipboard.writeText(text);
      showToast(successMessage);
      return;
    } catch (_) {
      /* 继续走 execCommand 兜底 */
    }
  }

  const area = document.createElement('textarea');
  area.value = text;
  area.setAttribute('readonly', '');
  area.style.position = 'fixed';
  area.style.top = '-1000px';
  area.style.opacity = '0';
  document.body.append(area);
  area.select();
  try {
    const ok = document.execCommand('copy');
    showToast(ok ? successMessage : '复制失败');
  } catch (_) {
    showToast('复制失败');
  }
  area.remove();
}

function shareTweet(card) {
  const url = tweetUrl(card);
  const title = card.handle ? `@${card.handle} 的推文` : '本地归档推文';
  if (navigator.share && window.matchMedia('(pointer: coarse)').matches) {
    navigator
      .share({ title, text: cut(card.text || '', 120), url })
      .catch(() => {});
    return;
  }
  copyText(url, '原帖链接已复制');
}

function copyTweetText(card) {
  const text = (card.text || '').trim();
  if (!text) {
    copyText(tweetUrl(card), '原帖链接已复制');
    return;
  }
  copyText(text, '推文文本已复制');
}

/* =========================================================
   备注抽屉
   ========================================================= */

function noteContextNode(card) {
  const wrap = h('div', { class: 'note-context' });
  const avatar = fillAvatar(
    h('span', { class: 'avatar', 'aria-hidden': 'true' }),
    card.avatar,
    card.name,
    card.handle
  );
  const copy = h('div', { class: 'note-context-copy' });
  const head = h('div', { class: 'note-context-head' });
  head.append(
    h('strong', { text: card.name || card.handle || '未知作者' }),
    h('span', { text: card.handle ? `@${card.handle}` : '' })
  );
  copy.append(
    head,
    h('p', { text: cut(card.text || '无正文', 120) })
  );
  wrap.append(avatar, copy);
  return wrap;
}

function openNote(card, trigger) {
  if (!card) return;
  closeTweetMenus();
  const localNote = noteFor(card.id);
  state.note = { card, trigger: trigger || null };
  els.noteContext.replaceChildren(noteContextNode(card));
  els.noteInput.value = localNote ? localNote.text : '';
  els.noteDelete.hidden = !localNote;
  updateNoteCount();
  sheetOpen(els.noteSheet, els.noteBackdrop);
  document.body.classList.add('is-locked');
  if (trigger) trigger.setAttribute('aria-expanded', 'true');
  els.noteInput.focus();
  els.noteInput.setSelectionRange(
    els.noteInput.value.length,
    els.noteInput.value.length
  );
  state.noteTrap = createFocusTrap(els.noteSheet, closeNote);
}

function closeNote() {
  if (!state.note) return;
  const trigger = state.note.trigger;
  state.note = null;
  sheetClose(els.noteSheet, els.noteBackdrop, () => Boolean(state.note));
  document.body.classList.remove('is-locked');
  if (state.noteTrap) {
    state.noteTrap();
    state.noteTrap = null;
  }
  if (trigger) {
    trigger.setAttribute('aria-expanded', 'false');
    if (typeof trigger.focus === 'function') trigger.focus();
  }
}

function noteListLabel(note, id) {
  if (note && note.handle) return `@${note.handle}`;
  if (note && note.name) return note.name;
  return `推文 ${id}`;
}

function renderNotesList(query = '') {
  if (!els.notesList) return;
  const needle = (query || '').trim().toLowerCase();
  const entries = Object.entries(state.notes || {})
    .filter(([, note]) => note && typeof note.text === 'string' && note.text.trim())
    .sort((a, b) => String(b[1].updated_at || '').localeCompare(String(a[1].updated_at || '')))
    .filter(([id, note]) => {
      if (!needle) return true;
      return (
        note.text.toLowerCase().includes(needle) ||
        id.includes(needle) ||
        String(note.handle || '').toLowerCase().includes(needle) ||
        String(note.name || '').toLowerCase().includes(needle)
      );
    });

  if (els.notesCount) {
    const total = Object.values(state.notes || {}).filter(
      (note) => note && typeof note.text === 'string' && note.text.trim()
    ).length;
    els.notesCount.textContent = needle
      ? `${entries.length} / ${total} 条备注`
      : `${total} 条备注`;
  }
  if (els.notesSearchClear) els.notesSearchClear.hidden = !needle;

  if (!entries.length) {
    const empty = h('div', { class: 'notes-empty' });
    empty.append(
      h('strong', { text: needle ? '没有匹配的备注' : '还没有本地备注' }),
      h('p', {
        text: needle
          ? '换个关键词，或清空搜索。'
          : '在推文动作行点回复气泡，写下的内容会出现在这里。',
      })
    );
    els.notesList.replaceChildren(empty);
    return;
  }

  const rows = entries.map(([id, note]) => {
    const row = h('div', { class: 'note-row' });
    row.append(h('p', { class: 'note-row-text', text: note.text }));
    const meta = h('div', { class: 'note-row-meta' });
    meta.append(
      h('span', { class: 'note-row-handle', text: noteListLabel(note, id) }),
      h('span', { text: note.updated_at ? relativeTime(note.updated_at) : '' })
    );
    row.append(meta);

    const actions = h('div', { class: 'note-row-actions' });
    actions.append(
      h('a', {
        href: `/tweet/${encodeURIComponent(id)}`,
        text: '打开推文',
      })
    );
    const remove = h('button', {
      type: 'button',
      class: 'note-row-delete',
      text: '删除',
      dataset: { noteDelete: id },
    });
    remove.addEventListener('click', () => {
      if (!window.confirm('删除这条本地备注？')) return;
      delete state.notes[id];
      writeLocalStore(NOTES_KEY, state.notes);
      const card = state.cards && state.cards.get(id);
      if (card) updateNoteUi(card);
      renderNotesList(els.notesSearch ? els.notesSearch.value : '');
      showToast('备注已删除');
    });
    actions.append(remove);
    row.append(actions);
    return row;
  });
  els.notesList.replaceChildren(...rows);
}

function openNotes(trigger) {
  if (!els.notesSheet || state.notesOpen) return;
  if (state.settingsOpen) {
    closeSettings();
    trigger = document.activeElement;
  }
  if (state.note) closeNote();
  state.notesOpen = true;
  state.notesTrigger = trigger || null;
  if (els.notesSearch) els.notesSearch.value = '';
  renderNotesList('');
  sheetOpen(els.notesSheet, els.notesBackdrop);
  document.body.classList.add('is-locked');
  if (els.notesSearch) els.notesSearch.focus();
  state.notesTrap = createFocusTrap(els.notesSheet, closeNotes);
}

function closeNotes() {
  if (!state.notesOpen) return;
  const trigger = state.notesTrigger;
  state.notesOpen = false;
  state.notesTrigger = null;
  sheetClose(els.notesSheet, els.notesBackdrop, () => state.notesOpen);
  document.body.classList.remove('is-locked');
  if (state.notesTrap) {
    state.notesTrap();
    state.notesTrap = null;
  }
  if (trigger && typeof trigger.focus === 'function' && trigger.offsetParent !== null) {
    trigger.focus();
  }
}

function updateNoteCount() {
  const length = els.noteInput.value.length;
  els.noteCount.textContent = `${length} / 500`;
}

function saveNote() {
  if (!state.note) return;
  const card = state.note.card;
  const value = els.noteInput.value.trim();

  if (!value) {
    if (noteFor(card.id)) {
      delete state.notes[card.id];
      writeLocalStore(NOTES_KEY, state.notes);
      updateNoteUi(card);
      closeNote();
      showToast('备注已删除');
    } else {
      showToast('写点什么再保存');
      els.noteInput.focus();
    }
    return;
  }

  state.notes[card.id] = {
    text: value,
    updated_at: new Date().toISOString(),
    handle: card.handle || '',
    name: card.name || '',
  };
  writeLocalStore(NOTES_KEY, state.notes);
  updateNoteUi(card);
  closeNote();
  showToast('备注已保存');
}

function deleteNote() {
  if (!state.note) return;
  const card = state.note.card;
  if (!noteFor(card.id)) return;
  delete state.notes[card.id];
  writeLocalStore(NOTES_KEY, state.notes);
  updateNoteUi(card);
  closeNote();
  showToast('备注已删除');
}

function updateNoteUi(card) {
  const article = document.getElementById(`tweet-${card.id}`);
  if (!article) return;
  const old = article.querySelector('.tweet-note');
  if (old) old.remove();

  const localNote = noteFor(card.id);
  const main = article.querySelector('.tweet-main');
  if (localNote && main) {
    const block = noteBlock(card);
    if (block) main.append(block);
  }

  const reply = article.querySelector('[data-action="reply"]');
  updateReplyButton(reply, Boolean(localNote));

  const menuNote = article.querySelector('[data-menu-action="note"] span');
  if (menuNote) {
    menuNote.textContent = localNote ? '编辑本地备注' : '添加本地备注';
  }
}

/* =========================================================
   推文更多菜单
   ========================================================= */

function closeTweetMenus(focusBack) {
  document.querySelectorAll('.tweet-menu').forEach((menu) => {
    if (menu.hidden) return;
    hidePopover(menu);
    const article = menu.closest('article.tweet');
    const trigger = article ? article.querySelector('[data-more]') : null;
    if (trigger) {
      trigger.setAttribute('aria-expanded', 'false');
      if (focusBack) trigger.focus();
    }
  });
  state.moreOpenId = null;
  state.moreTrigger = null;
}

function openTweetMenu(article, trigger) {
  if (!article) return;
  closeTweetMenus();
  const menu = article.querySelector('.tweet-menu');
  if (!menu) return;
  showPopover(menu);
  trigger.setAttribute('aria-expanded', 'true');
  state.moreOpenId = article.dataset.tweetId;
  state.moreTrigger = trigger;
  const first = menu.querySelector('[role="menuitem"]');
  if (first) first.focus();
}

function menuItems(menu) {
  return Array.from(menu.querySelectorAll('[role="menuitem"]'));
}

function handleMenuKeydown(event) {
  if (!state.moreOpenId) return;
  const menu = document.querySelector('.tweet-menu:not([hidden])');
  if (!menu) return;
  const items = menuItems(menu);
  const index = items.indexOf(document.activeElement);

  if (event.key === 'Escape') {
    event.preventDefault();
    event.stopPropagation();
    closeTweetMenus(true);
    return;
  }
  if (event.key === 'Tab') {
    closeTweetMenus(false);
    return;
  }
  if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return;
  event.preventDefault();
  let next = index;
  if (event.key === 'ArrowDown') next = index < 0 ? 0 : (index + 1) % items.length;
  else if (event.key === 'ArrowUp') {
    next = index <= 0 ? items.length - 1 : index - 1;
  } else if (event.key === 'Home') next = 0;
  else next = items.length - 1;
  if (items[next]) items[next].focus();
}

function handleTweetMenu(card, action, article) {
  if (!card) return;
  if (action === 'detail') {
    window.location.assign(`/tweet/${encodeURIComponent(card.id)}`);
    return;
  }
  if (action === 'copy-text') {
    copyTweetText(card);
    return;
  }
  if (action === 'copy-link') {
    copyText(tweetUrl(card), '原帖链接已复制');
    return;
  }
  if (action === 'author') {
    if (card.handle) setAuthor(card.handle);
    return;
  }
  if (action === 'note') {
    const trigger = article
      ? article.querySelector('[data-action="reply"]')
      : null;
    openNote(card, trigger);
  }
}

/* =========================================================
   键盘浏览与快捷键
   ========================================================= */

function isTypingTarget(target) {
  return (
    target instanceof Element &&
    Boolean(target.closest('input, textarea, select, [contenteditable="true"]'))
  );
}

function hasOpenDialog() {
  return Boolean(state.settingsOpen || state.lightbox || state.note || state.notesOpen);
}

function visibleArticles() {
  return Array.from(els.feed.querySelectorAll('article.tweet'));
}

function focusedArticle() {
  if (state.focusedId) {
    const exact = document.getElementById(`tweet-${state.focusedId}`);
    if (exact) return exact;
  }
  const articles = visibleArticles();
  if (!articles.length) return null;
  const target = window.innerHeight * 0.22;
  return (
    articles.find((article) => {
      const rect = article.getBoundingClientRect();
      return rect.bottom > target;
    }) || articles[0]
  );
}

function focusedCard() {
  const article = focusedArticle();
  if (!article) return null;
  return state.cards.get(article.dataset.tweetId) || null;
}

function focusArticle(article) {
  if (!article) return;
  document
    .querySelectorAll('article.tweet.is-focused')
    .forEach((node) => node.classList.remove('is-focused'));
  article.classList.add('is-focused');
  state.focusedId = article.dataset.tweetId;
  article.focus({ preventScroll: true });
  article.scrollIntoView({
    block: 'center',
    behavior: state.reduceMotion ? 'auto' : 'smooth',
  });
}

function moveTweetFocus(direction) {
  const articles = visibleArticles();
  if (!articles.length) return;
  let index = -1;
  if (state.focusedId) {
    index = articles.findIndex(
      (article) => article.dataset.tweetId === state.focusedId
    );
  }
  if (index < 0) {
    focusArticle(focusedArticle() || articles[0]);
    return;
  }
  const next = Math.max(0, Math.min(articles.length - 1, index + direction));
  focusArticle(articles[next]);
}

function focusSearchInput() {
  const input = els.searchInputs.find(
    (node) => node.offsetParent !== null && !node.disabled
  );
  if (!input) return;
  input.focus();
  input.select();
}

function clearSearchFromKeyboard() {
  if (!state.q && !state.author) return false;
  clearSearch();
  return true;
}

function mediaTiles() {
  if (!els.mediaGrid) return [];
  return Array.from(els.mediaGrid.querySelectorAll('button.media-tile'));
}

function moveMediaFocus(direction) {
  const tiles = mediaTiles();
  if (!tiles.length) return;
  const index = tiles.indexOf(document.activeElement);
  if (index < 0) {
    const first = direction > 0 ? tiles[0] : tiles[tiles.length - 1];
    first.focus();
    first.scrollIntoView({
      block: 'nearest',
      behavior: state.reduceMotion ? 'auto' : 'smooth',
    });
    return;
  }
  const next = Math.max(0, Math.min(tiles.length - 1, index + direction));
  tiles[next].focus();
  tiles[next].scrollIntoView({
    block: 'nearest',
    behavior: state.reduceMotion ? 'auto' : 'smooth',
  });
}

function handleGlobalShortcut(event) {
  if (event.defaultPrevented) return;
  if (event.metaKey || event.ctrlKey || event.altKey) return;
  if (isTypingTarget(event.target)) return;
  if (hasOpenDialog()) return;

  if (state.moreOpenId) return;

  if (state.gPending) {
    state.gPending = false;
    if (state.gTimer) window.clearTimeout(state.gTimer);
    const key = event.key.toLowerCase();
    const routes = { h: '/', m: '/media', f: '/favorites', s: '/stats' };
    if (routes[key]) {
      event.preventDefault();
      window.location.assign(routes[key]);
      return;
    }
  }

  if (event.key.toLowerCase() === 'g' && !event.shiftKey) {
    event.preventDefault();
    state.gPending = true;
    if (state.gTimer) window.clearTimeout(state.gTimer);
    state.gTimer = window.setTimeout(() => {
      state.gPending = false;
    }, 900);
    return;
  }

  const key = event.key.toLowerCase();

  if (key === 'j' || key === 'k') {
    event.preventDefault();
    if (VIEW === 'media') moveMediaFocus(key === 'j' ? 1 : -1);
    else moveTweetFocus(key === 'j' ? 1 : -1);
    return;
  }

  if (event.key === '/') {
    event.preventDefault();
    focusSearchInput();
    return;
  }

  if (event.key === '?') {
    event.preventDefault();
    openSettings(document.activeElement);
    return;
  }

  if (key === 'n') {
    event.preventDefault();
    openNotes(document.activeElement);
    return;
  }

  if (event.key === 'Escape') {
    if (clearSearchFromKeyboard()) {
      event.preventDefault();
      return;
    }
    closeTweetMenus(false);
    return;
  }

  if (!['r', 'b', 'l', 't', 's'].includes(key)) return;
  const card = focusedCard();
  if (!card) return;
  const article = focusedArticle();
  if (!article) return;

  if (key === 'r') {
    event.preventDefault();
    openNote(card, article.querySelector('[data-action="reply"]'));
    return;
  }

  if (key === 'b') {
    event.preventDefault();
    const button = article.querySelector('[data-bookmark]');
    if (button) toggleFavorite(card.id, button);
    return;
  }

  if (key === 's') {
    event.preventDefault();
    shareTweet(card);
    return;
  }

  if (key === 'l' || key === 't') {
    event.preventDefault();
    const button = article.querySelector(`[data-action="${key === 'l' ? 'like' : 'repost'}"]`);
    if (button) toggleLocalMark(card, key === 'l' ? 'like' : 'repost', button);
  }
}

/* =========================================================
   已读
   ========================================================= */

function setupSeenObserver() {
  if (VIEW === 'stats' || !('IntersectionObserver' in window)) return;

  if (!state.seenObserver) {
    state.seenObserver = new IntersectionObserver(
      (entries) => {
        if (document.hidden) return;
        for (const entry of entries) {
          const article = entry.target;
          const id = article.dataset.tweetId;
          if (!id || article.dataset.seen === '1') continue;
          const visible =
            entry.isIntersecting &&
            (entry.intersectionRatio >= 0.6 ||
              entry.intersectionRect.height >= window.innerHeight * 0.6);

          if (visible) {
            if (!state.seenTimers.has(id)) {
              const timer = window.setTimeout(() => {
                state.seenTimers.delete(id);
                markSeen(id, article);
              }, 1500);
              state.seenTimers.set(id, timer);
            }
          } else {
            const timer = state.seenTimers.get(id);
            if (timer) {
              window.clearTimeout(timer);
              state.seenTimers.delete(id);
            }
          }
        }
      },
      { threshold: [0, 0.2, 0.6, 1] }
    );
  }

  state.seenObserver.disconnect();
  els.feed.querySelectorAll('article.tweet').forEach((article) => {
    if (article.dataset.seen !== '1') state.seenObserver.observe(article);
  });
}

function resetSeenObserver() {
  if (state.seenObserver) state.seenObserver.disconnect();
  for (const timer of state.seenTimers.values()) window.clearTimeout(timer);
  state.seenTimers.clear();
}

function markSeen(id, article) {
  if (!state.sessionId || document.hidden) return;
  if (!article || article.dataset.seen === '1') return;
  article.dataset.seen = '1';
  const card = state.cards.get(id);
  if (card) card.seen = true;
  state.seenPending.add(id);
  scheduleSeenFlush(500);
}

function scheduleSeenFlush(delay) {
  if (state.seenFlushTimer) window.clearTimeout(state.seenFlushTimer);
  state.seenFlushTimer = window.setTimeout(flushSeen, delay);
}

async function flushSeen(useBeacon) {
  if (!state.sessionId || !state.seenPending.size) return;

  const ids = Array.from(state.seenPending).slice(0, 500);
  if (useBeacon) {
    beaconJSON('/api/state/seen', {
      session_id: state.sessionId,
      tweet_ids: ids,
    });
    return;
  }
  if (state.seenSending) return;
  state.seenSending = true;

  try {
    const response = await fetch('/api/state/seen', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        session_id: state.sessionId,
        tweet_ids: ids,
      }),
      keepalive: true,
    });
    if (response.ok || response.status >= 400) {
      ids.forEach((id) => state.seenPending.delete(id));
    }
  } catch (_) {
    /* 网络失败：保留，稍后重试 */
  } finally {
    state.seenSending = false;
    if (state.seenPending.size) scheduleSeenFlush(4000);
  }
}

/* =========================================================
   头部 / 筛选 / 搜索 / 排序
   ========================================================= */

function updateHead() {
  if (VIEW === 'stats') {
    els.headMeta.textContent = '观看、播放与收藏';
    return;
  }
  if (VIEW === 'media') {
    updateMediaHead();
    return;
  }
  if (VIEW === 'tweet') {
    els.headMeta.textContent = state.detailChildCount
      ? `推文 · 线程 ${num(state.detailChildCount)}`
      : '推文详情';
    return;
  }
  if (VIEW === 'author') {
    const name = state.authorName ? `${state.authorName} ` : '';
    const accountBit = state.account ? ` · 账号 ${accountTag(state.account)}` : '';
    els.headMeta.textContent = `${name}@${state.author}${accountBit} · ${num(state.total)} 条推文`;
    return;
  }

  const totalLabel = `${num(state.total)} 条`;
  const bits = [];
  if (state.account) bits.push(`账号 ${accountTag(state.account)}`);
  if (state.author) bits.push(`@${state.author}`);
  else if (state.q) bits.push(`搜索 “${cut(state.q, 22)}”`);
  if (state.mediaType !== 'all') bits.push(MEDIA_TYPE_LABELS[state.mediaType]);
  if (state.source !== 'all' && VIEW !== 'favorites') {
    bits.push(SOURCE_LABELS[state.source] || state.source);
  }
  if (state.readState !== 'all') bits.push(READ_STATE_LABELS[state.readState]);
  if (state.sort !== 'created') bits.push(SORT_LABELS[state.sort] || '排序');
  if (advancedFilterActive()) bits.push('高级筛选');

  if (VIEW === 'favorites') {
    const parts = ['收藏', ...bits];
    els.headMeta.textContent = `${parts.join(' · ')} · ${totalLabel}`;
    return;
  }

  if (bits.length) {
    els.headMeta.textContent = `${bits.join(' · ')} · ${totalLabel}`;
    return;
  }

  let oldest = state.meta && state.meta.oldest ? state.meta.oldest : null;
  let newest = state.meta && state.meta.newest ? state.meta.newest : null;
  let range = '';
  if (oldest && newest) {
    const first = yearOf(oldest);
    const last = yearOf(newest);
    range = first === last ? `${first}` : `${first} — ${last}`;
  }
  els.headMeta.textContent = `${num(state.total)} 条推文${range ? ` · ${range}` : ''}`;
}

function updateFilterUi() {
  if (!els.tabs) return;
  els.tabs.querySelectorAll('[data-media-type]').forEach((tab) => {
    const active = tab.dataset.mediaType === state.mediaType;
    tab.classList.toggle('is-active', active);
    tab.setAttribute('aria-pressed', active ? 'true' : 'false');
  });
}

function updateAuthorContext() {
  if (!els.filterContext) return;
  const visible =
    Boolean(state.author) && VIEW !== 'author' && VIEW !== 'tweet';
  els.filterContext.hidden = !visible;
  if (state.author) els.filterAuthor.textContent = `@${state.author}`;
}

function syncSearchInputs(value) {
  const next =
    typeof value === 'string'
      ? value
      : VIEW === 'author'
        ? state.q
        : state.author
          ? `@${state.author}`
          : state.q;
  els.searchInputs.forEach((input) => {
    if (input.value !== next) input.value = next;
  });
  updateSearchClear();
}

function updateSearchClear() {
  els.searchClears.forEach((button) => {
    const field = button.closest('.search-field');
    const input = field ? field.querySelector('input') : null;
    button.hidden = !input || !input.value;
  });
}

function updateSearchInputs(value) {
  els.searchInputs.forEach((input) => {
    input.value = value;
  });
  updateSearchClear();
}

function syncUrl() {
  if (VIEW === 'stats' || VIEW === 'tweet') return;
  const url = new URL(window.location.href);
  const query = new URLSearchParams();
  const theme = url.searchParams.get('theme');
  if (theme) query.set('theme', theme);

  if (VIEW === 'media') {
    if (state.media.filter !== 'all') query.set('filter', state.media.filter);
  }
  if (state.account && FEED_VIEWS.includes(VIEW)) {
    query.set('account', state.account);
  }
  // 四维正交筛选写进 URL：media_type / source / read_state。
  // 收藏页的来源固定是「本地收藏」，由路径表达，不再重复写。
  if (state.mediaType !== 'all') query.set('media_type', state.mediaType);
  if (state.source !== 'all' && VIEW !== 'favorites') {
    query.set('source', state.source);
  }
  if (state.readState !== 'all') query.set('read_state', state.readState);
  if (state.sort !== 'created') query.set('sort', state.sort);
  if (state.q) query.set('q', state.q);
  if (state.author && VIEW !== 'author') query.set('author', state.author);
  appendAdvancedParams(query);

  const suffix = query.toString();
  const path =
    VIEW === 'favorites'
      ? '/favorites'
      : VIEW === 'media'
        ? '/media'
        : VIEW === 'author'
          ? `/author/${encodeURIComponent(state.author)}`
          : '/';
  window.history.replaceState(null, '', `${path}${suffix ? `?${suffix}` : ''}`);
}

/* ---------- 高级筛选：来源 / 阅读状态 / 排序 + 日期 / 最低互动 ---------- */

function setChipPressed(button, active) {
  button.classList.toggle('is-active', active);
  button.setAttribute('aria-pressed', active ? 'true' : 'false');
}

// 把 state 里的三个选择同步进面板暂存态，并刷新按钮选中态。
function fillFilterDraft() {
  filterDraft.source = state.source;
  filterDraft.readState = state.readState;
  filterDraft.sort = state.sort;
  renderFilterDraft();
}

function renderFilterDraft() {
  if (!els.filterPanel) return;
  els.filterPanel.querySelectorAll('[data-source]').forEach((button) => {
    setChipPressed(button, button.dataset.source === filterDraft.source);
  });
  els.filterPanel.querySelectorAll('[data-read-state]').forEach((button) => {
    setChipPressed(button, button.dataset.readState === filterDraft.readState);
  });
  els.filterPanel.querySelectorAll('[data-sort]').forEach((button) => {
    setChipPressed(button, button.dataset.sort === filterDraft.sort);
  });
}

function advancedFilterActive() {
  return Boolean(state.since || state.until || state.minLikes || state.minViews);
}

// 头部「高级筛选」徽标的计数：来源 / 阅读状态 / 排序 + 日期与最低互动。
function activeFilterCount() {
  let count = 0;
  if (VIEW !== 'favorites' && state.source !== 'all') count += 1;
  if (state.readState !== 'all') count += 1;
  if (state.sort !== 'created') count += 1;
  if (state.since) count += 1;
  if (state.until) count += 1;
  if (state.minLikes) count += 1;
  if (state.minViews) count += 1;
  return count;
}

function updateAdvancedUi() {
  if (els.filterSince) els.filterSince.value = state.since || '';
  if (els.filterUntil) els.filterUntil.value = state.until || '';
  if (els.filterMinLikes) els.filterMinLikes.value = state.minLikes || '';
  if (els.filterMinViews) els.filterMinViews.value = state.minViews || '';
  fillFilterDraft();
  if (!els.filterBtn) return;
  const count = activeFilterCount();
  const active = count > 0;
  if (els.filterCount) {
    els.filterCount.textContent = active ? String(count) : '';
    els.filterCount.hidden = !active;
  }
  const parts = [];
  if (VIEW !== 'favorites' && state.source !== 'all') {
    parts.push(SOURCE_LABELS[state.source] || state.source);
  }
  if (state.readState !== 'all') parts.push(READ_STATE_LABELS[state.readState]);
  if (state.sort !== 'created') parts.push(SORT_LABELS[state.sort] || '排序');
  if (state.since) parts.push(`从 ${state.since}`);
  if (state.until) parts.push(`到 ${state.until}`);
  if (state.minLikes) parts.push(`喜欢 ≥ ${state.minLikes}`);
  if (state.minViews) parts.push(`查看 ≥ ${state.minViews}`);
  const label = active ? `高级筛选：${parts.join(' · ')}` : '高级筛选';
  els.filterBtn.classList.toggle('is-active', active);
  els.filterBtn.setAttribute('aria-pressed', active ? 'true' : 'false');
  els.filterBtn.setAttribute('aria-label', label);
  els.filterBtn.title = label;
}

function setFilterPanel(open) {
  if (!els.filterPanel || !els.filterBtn) return;
  const wasOpen = !els.filterPanel.hidden;
  els.filterPanel.hidden = !open;
  els.filterBtn.setAttribute('aria-expanded', open ? 'true' : 'false');
  if (!open || wasOpen) return;
  // 打开面板时把当前 state 同步到控件；搜索历史浮层让位。
  updateAdvancedUi();
  hideSearchHistory();
  const activeChips = Array.from(
    els.filterPanel.querySelectorAll('.filter-chip.is-active')
  );
  const first =
    activeChips.find((node) => node.offsetParent !== null) || els.filterSince;
  if (first) first.focus();
}

function clearAdvancedFilters() {
  state.since = '';
  state.until = '';
  state.minLikes = '';
  state.minViews = '';
  if (VIEW !== 'favorites') state.source = 'all';
  state.readState = 'all';
  state.sort = 'created';
  updateAdvancedUi();
  setFilterPanel(false);
  syncUrl();
  reloadCurrent();
}

// 收藏页来源固定为「本地收藏」：选了别的来源就跳到时间线的同条件视图。
function favoritesToTimelineUrl(since, until, minLikes, minViews) {
  const query = new URLSearchParams();
  if (filterDraft.source !== 'all') query.set('source', filterDraft.source);
  if (filterDraft.readState !== 'all') query.set('read_state', filterDraft.readState);
  if (filterDraft.sort !== 'created') query.set('sort', filterDraft.sort);
  if (state.mediaType !== 'all') query.set('media_type', state.mediaType);
  if (state.q) query.set('q', state.q);
  if (state.account) query.set('account', state.account);
  if (state.author) query.set('author', state.author);
  if (since) query.set('since', since);
  if (until) query.set('until', until);
  if (minLikes > 0) query.set('min_likes', String(minLikes));
  if (minViews > 0) query.set('min_views', String(minViews));
  const suffix = query.toString();
  return `/${suffix ? `?${suffix}` : ''}`;
}

function applyAdvancedFilters() {
  const since = els.filterSince ? els.filterSince.value : '';
  const until = els.filterUntil ? els.filterUntil.value : '';
  if (since && until && since > until) {
    showToast('开始日期不能晚于结束日期');
    return;
  }
  const minLikes = els.filterMinLikes ? Number.parseInt(els.filterMinLikes.value, 10) : 0;
  const minViews = els.filterMinViews ? Number.parseInt(els.filterMinViews.value, 10) : 0;
  const likes = Number.isFinite(minLikes) && minLikes > 0 ? String(minLikes) : '';
  const views = Number.isFinite(minViews) && minViews > 0 ? String(minViews) : '';
  if (VIEW === 'favorites' && filterDraft.source !== 'local_fav') {
    window.location.assign(favoritesToTimelineUrl(since, until, minLikes, minViews));
    return;
  }
  // 面板内点选只进暂存态；点「应用」才写回 state。
  if (VIEW !== 'favorites') state.source = filterDraft.source;
  state.readState = filterDraft.readState;
  state.sort = filterDraft.sort;
  state.since = since || '';
  state.until = until || '';
  state.minLikes = likes;
  state.minViews = views;
  updateAdvancedUi();
  setFilterPanel(false);
  syncUrl();
  reloadCurrent();
}

function searchHistoryPanelFor(input) {
  const wrap = input && input.closest('.search-field');
  const panel = wrap && wrap.nextElementSibling;
  return panel && panel.matches('[data-search-history]') ? panel : null;
}

function renderSearchHistory(input) {
  if (!els.searchHistories.length) return;
  const history = state.searchHistory || [];
  hideSearchHistory();
  if (!input || input.value.trim() || !history.length) return;
  const panel = searchHistoryPanelFor(input);
  if (!panel) return;

  const head = h('div', { class: 'search-history-head' });
  head.append(h('span', { text: '最近搜索' }));
  const clear = h('button', { type: 'button', text: '清除' });
  clear.addEventListener('pointerdown', (event) => event.preventDefault());
  clear.addEventListener('click', (event) => {
    event.preventDefault();
    state.searchHistory = [];
    writeSearchHistory([]);
    hideSearchHistory();
  });
  head.append(clear);

  panel.replaceChildren(head);
  panel.setAttribute('aria-label', '最近搜索');
  const items = [];
  state.searchHistory.forEach((query) => {
    const button = h('button', {
      type: 'button',
      class: 'search-history-item',
      title: query,
    });
    button.append(icon('search'), h('span', { text: query }));
    button.addEventListener('pointerdown', (event) => event.preventDefault());
    button.addEventListener('click', () => {
      els.searchInputs.forEach((node) => {
        node.value = query;
      });
      state.q = query;
      if (state.author && VIEW !== 'author') {
        state.author = '';
        updateAuthorContext();
      }
      recordSearch(query);
      updateSearchClear();
      hideSearchHistory();
      syncUrl();
      reloadCurrent();
    });
    button.addEventListener('keydown', (event) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        input.focus();
        return;
      }
      const offset = event.key === 'ArrowDown' ? 1 : event.key === 'ArrowUp' ? -1 : 0;
      if (!offset) return;
      event.preventDefault();
      const index = items.indexOf(button);
      const next = items[index + offset];
      if (next) next.focus();
      else if (offset < 0) input.focus();
    });
    items.push(button);
    panel.append(button);
  });
  if (!panel.dataset.historyBound) {
    panel.dataset.historyBound = '1';
    panel.addEventListener('focusout', () => {
      window.setTimeout(() => {
        if (!panel.contains(document.activeElement)) hideSearchHistory();
      }, 80);
    });
  }
  panel.hidden = false;
  showPopover(panel);
}

function hideSearchHistory() {
  els.searchHistories.forEach((panel) => {
    hidePopover(panel);
  });
}

function recordSearch(query) {
  const value = (query || '').trim();
  if (!value) return;
  state.searchHistory = [
    value,
    ...(state.searchHistory || []).filter((item) => item !== value),
  ].slice(0, 8);
  writeSearchHistory(state.searchHistory);
}

function clearSearch() {
  hideSearchHistory();
  state.q = '';
  if (VIEW !== 'author') state.author = '';
  updateAuthorContext();
  updateSearchInputs('');
  syncUrl();
  reloadCurrent();
}

function jumpToMedia(kind) {
  let selector = '.tweet-media';
  if (kind === 'video') selector = '.tweet-media .media-cell.is-video';
  else if (kind === 'grid') {
    selector =
      '.tweet-media .media-grid[data-count="2"], .tweet-media .media-grid[data-count="3"], .tweet-media .media-grid[data-count="4"]';
  }
  const target = document.querySelector(selector);
  if (!target) return;
  const tweet = target.closest('.tweet') || target;
  const top = tweet.getBoundingClientRect().top + window.scrollY - 78;
  window.scrollTo({ top: Math.max(0, top), behavior: 'auto' });
}

/* =========================================================
   右栏
   ========================================================= */

function formatJoined(value) {
  if (!value) return '';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return `${date.getFullYear()}年${date.getMonth() + 1}月`;
}

function ownerJoined(value) {
  if (!value) return '—';
  return formatJoined(value);
}

function renderOwner(meta) {
  const owner = meta && meta.owner;
  if (!owner || (!owner.username && !owner.display_name)) {
    if (els.sideOwner) els.sideOwner.hidden = true;
    return;
  }
  const name = owner.display_name || owner.username || '本地归档';
  const handle = owner.username || 'local';
  fillAvatar(els.ownerAvatar, owner.avatar, name, handle);
  if (els.ownerName) els.ownerName.textContent = name;
  if (els.ownerHandle) els.ownerHandle.textContent = `@${handle}`;
  if (els.sideOwnerName) els.sideOwnerName.textContent = name;
  if (els.sideOwnerHandle) els.sideOwnerHandle.textContent = `@${handle}`;
  if (els.ownerFollowers) {
    els.ownerFollowers.textContent =
      owner.followers_count == null ? '—' : num(owner.followers_count);
  }
  if (els.ownerFollowing) {
    els.ownerFollowing.textContent =
      owner.following_count == null ? '—' : num(owner.following_count);
  }
  if (els.ownerJoined) els.ownerJoined.textContent = ownerJoined(owner.joined_at);

  const flags = [];
  if (owner.verified) flags.push('认证账号');
  if (owner.protected) flags.push('私密账号');
  if (els.ownerFlags) {
    els.ownerFlags.hidden = flags.length === 0;
    els.ownerFlags.textContent = flags.join(' · ');
  }

  const syncParts = [];
  const sync = meta.sync || {};
  for (const [key, label] of [
    ['like', '喜欢'],
    ['bookmark', '书签'],
    ['tweet', '时间线'],
  ]) {
    if (sync[key] && sync[key].updated_at) {
      syncParts.push(`${label} ${dayStamp(sync[key].updated_at)}`);
    }
  }
  const ops = meta.raw_operations || {};
  const opLabels = {
    Likes: '喜欢',
    Bookmarks: '书签',
    ThreadExpandDetail: '线程展开',
    UserTweets: '用户推文',
  };
  const opParts = Object.entries(ops)
    .filter(([, value]) => value > 0)
    .map(([key, value]) => `${opLabels[key] || key} ${value}`);
  if (opParts.length) syncParts.push(`采集 ${opParts.join(' · ')}`);
  if (els.ownerSync) {
    els.ownerSync.hidden = syncParts.length === 0;
    els.ownerSync.textContent = syncParts.join(' · ');
  }

  if (els.sideOwner) els.sideOwner.hidden = false;
  const account = document.querySelector('.account');
  if (account) account.title = `${name} @${handle} · 本地归档，只读`;
}

async function loadOwnerProfile() {
  try {
    const meta = await loadMeta();
    renderOwner(meta);
  } catch (error) {
    /* 演示/离线模式下保留默认本地归档账号 */
  }
}

function renderSide(meta, page) {
  const metaTotal =
    meta && meta.counts && meta.counts.tweets != null
      ? Number(meta.counts.tweets)
      : NaN;
  const total = Number.isFinite(metaTotal)
    ? metaTotal
    : Number(page.total) || 0;
  els.bigStat.classList.remove('is-loading');
  els.statTweets.textContent = num(total);

  let oldest = meta && meta.oldest ? meta.oldest : null;
  let newest = meta && meta.newest ? meta.newest : null;
  if ((!oldest || !newest) && page.items && page.items.length) {
    newest = newest || page.items[0].created;
    oldest = oldest || page.items[page.items.length - 1].created;
  }

  if (oldest && newest) {
    els.sideRange.textContent = `${monthStamp(oldest)} — ${monthStamp(newest)}`;
    els.metaOldest.textContent = dayStamp(oldest);
    els.metaNewest.textContent = dayStamp(newest);
  } else {
    els.sideRange.textContent = '暂无时间信息';
    els.metaOldest.textContent = '—';
    els.metaNewest.textContent = '—';
  }
  els.metaGenerated.textContent =
    meta && meta.generated_at ? dayStamp(meta.generated_at) : '—';

  const types = (meta && meta.media_types) || {};
  const photo = Number(types.photo) || 0;
  const video = Number(types.video) || 0;
  const gif = Number(types.animated_gif) || 0;
  els.countPhoto.textContent = types.photo == null ? '—' : num(photo);
  els.countVideo.textContent = types.video == null ? '—' : num(video);
  els.countGif.textContent = types.animated_gif == null ? '—' : num(gif);
  renderMediaBar(photo, video, gif);
}

function renderMediaBar(photo, video, gif) {
  els.mediaBar.replaceChildren();
  const total = photo + video + gif;
  if (total <= 0) return;

  for (const [count, className] of [
    [photo, 'bar-photo'],
    [video, 'bar-video'],
    [gif, 'bar-gif'],
  ]) {
    if (count <= 0) continue;
    const segment = h('span', { class: className });
    segment.style.flex = `${count} 1 0`;
    els.mediaBar.append(segment);
  }
}

function renderSideLoading() {
  els.bigStat.classList.add('is-loading');
  els.statTweets.textContent = '';
  els.sideRange.textContent = '读取归档信息…';
  els.countPhoto.textContent = '—';
  els.countVideo.textContent = '—';
  els.countGif.textContent = '—';
  els.metaOldest.textContent = '—';
  els.metaNewest.textContent = '—';
  els.metaGenerated.textContent = '—';
  els.mediaBar.replaceChildren();
}

function renderSideEmpty() {
  els.bigStat.classList.remove('is-loading');
  els.statTweets.textContent = '0';
  els.sideRange.textContent = '暂无推文';
  els.mediaBar.replaceChildren();
  els.countPhoto.textContent = '0';
  els.countVideo.textContent = '0';
  els.countGif.textContent = '0';
  els.metaOldest.textContent = '—';
  els.metaNewest.textContent = '—';
  els.metaGenerated.textContent = '—';
}

/* =========================================================
   设置
   ========================================================= */

function applySettings() {
  state.autoplay = readSetting('timeline:autoplay', false);
  state.reduceMotion = readSetting('timeline:reduce-motion', prefersReducedMotion());
  document.documentElement.classList.toggle('reduce-motion', state.reduceMotion);
  updateSettingsUi();
}

function updateSettingsUi() {
  els.autoplaySwitch.setAttribute('aria-checked', state.autoplay ? 'true' : 'false');
  els.motionSwitch.setAttribute('aria-checked', state.reduceMotion ? 'true' : 'false');
  updateThemeRows();
}

function updateThemeRows() {
  const current = document.documentElement.getAttribute('data-theme') || 'dark';
  els.themeRows.forEach((row) => {
    const checked = row.dataset.themeChoice === current;
    row.setAttribute('aria-checked', checked ? 'true' : 'false');
    row.tabIndex = checked ? 0 : -1;
  });
}

function openSettings(trigger) {
  if (state.settingsOpen) return;
  state.settingsOpen = true;
  state.settingsTrigger = trigger || null;
  updateThemeRows();
  updateSettingsUi();
  sheetOpen(els.settingsSheet, els.settingsBackdrop);
  document.body.classList.add('is-locked');
  document.querySelectorAll('[data-open-settings]').forEach((node) => {
    node.setAttribute('aria-expanded', 'true');
  });
  const focusTarget =
    els.settingsSheet.querySelector('[data-autofocus]') ||
    els.settingsSheet.querySelector('button');
  if (focusTarget) focusTarget.focus();
  state.settingsTrap = createFocusTrap(els.settingsSheet, closeSettings);
}

function downloadJsonFile(data, filename) {
  const blob = new Blob([JSON.stringify(data, null, 2)], {
    type: 'application/json',
  });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  document.body.append(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

async function exportState() {
  try {
    const data = await getJSON('/api/state/export');
    downloadJsonFile(data, `timeline-state-${dateKey(new Date())}.json`);
    showToast('状态已导出');
  } catch (error) {
    showToast(`导出失败：${error.message}`);
  }
}

async function markAllSeen() {
  if (!window.confirm('把当前归档里的所有推文都标为已读？')) return;
  try {
    const result = await postJSON('/api/state/seen-all', {});
    showToast(`已标记 ${num(result.seen_total)} 条推文为已读`);
    if (VIEW === 'stats') loadStats();
    else if (VIEW !== 'tweet' && VIEW !== 'media') reloadFeed();
  } catch (error) {
    showToast(`标记失败：${error.message}`);
  }
}

function exportLocalData() {
  downloadJsonFile(
    {
      version: 1,
      exported_at: new Date().toISOString(),
      notes: state.notes,
      marks: state.marks,
    },
    `timeline-local-${dateKey(new Date())}.json`
  );
  showToast('本地备注与标记已导出');
}

async function importLocalFile(event) {
  const file = event.target.files && event.target.files[0];
  event.target.value = '';
  if (!file) return;
  try {
    const data = JSON.parse(await file.text());
    if (
      !data ||
      typeof data.notes !== 'object' ||
      Array.isArray(data.notes) ||
      typeof data.marks !== 'object' ||
      Array.isArray(data.marks)
    ) {
      throw new Error('JSON 结构不对');
    }
    if (!window.confirm('导入会覆盖当前浏览器里的备注和喜欢/转发标记，继续？')) {
      return;
    }
    state.notes = data.notes;
    state.marks = data.marks;
    writeLocalStore(NOTES_KEY, state.notes);
    writeLocalStore(MARKS_KEY, state.marks);
    showToast('本地备注与标记已导入');
    if (VIEW === 'tweet') loadTweetDetail();
    else if (VIEW !== 'media' && VIEW !== 'stats') reloadFeed();
  } catch (error) {
    showToast(`导入失败：${error.message}`);
  }
}

async function resetState() {
  const confirmed = window.confirm(
    '清空已读、播放、会话和每日统计？收藏不会删除，此操作不可撤销。'
  );
  if (!confirmed) return;
  try {
    await postJSON('/api/state/reset', { confirm: 'yes' });
    showToast('观看统计已清空');
    if (VIEW === 'stats') loadStats();
    else if (VIEW !== 'tweet' && VIEW !== 'media') reloadFeed();
  } catch (error) {
    showToast(`清空失败：${error.message}`);
  }
}

function closeSettings() {
  if (!state.settingsOpen) return;
  state.settingsOpen = false;
  sheetClose(els.settingsSheet, els.settingsBackdrop, () => state.settingsOpen);
  document.querySelectorAll('[data-open-settings]').forEach((node) => {
    node.setAttribute('aria-expanded', 'false');
  });
  document.body.classList.remove('is-locked');
  if (state.settingsTrap) {
    state.settingsTrap();
    state.settingsTrap = null;
  }
  if (state.settingsTrigger && typeof state.settingsTrigger.focus === 'function') {
    state.settingsTrigger.focus();
  }
}

function createFocusTrap(container, onEscape) {
  const selector =
    'button, [href], input, select, textarea, video[controls], [tabindex]:not([tabindex="-1"])';

  function focusables() {
    return Array.from(container.querySelectorAll(selector)).filter(
      (node) =>
        !node.disabled &&
        node.getAttribute('aria-hidden') !== 'true' &&
        (node.offsetWidth > 0 || node.offsetHeight > 0)
    );
  }

  function onKey(event) {
    if (event.key === 'Escape') {
      event.preventDefault();
      onEscape();
      return;
    }
    if (event.key !== 'Tab') return;
    const nodes = focusables();
    if (!nodes.length) return;
    const first = nodes[0];
    const last = nodes[nodes.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  document.addEventListener('keydown', onKey, true);
  return () => document.removeEventListener('keydown', onKey, true);
}

/* =========================================================
   灯箱
   ========================================================= */

function openLightbox(card, index, trigger) {
  if (!card || !Array.isArray(card.media) || !card.media[index]) return;
  document.querySelectorAll('.media-cell video').forEach((video) => video.pause());
  if (state.lightboxCloseTimer) {
    window.clearTimeout(state.lightboxCloseTimer);
    state.lightboxCloseTimer = null;
  }
  state.lightbox = { card, index, trigger: trigger || null, video: null };
  els.lightbox.hidden = false;
  document.body.classList.add('is-locked');
  renderLightbox();
  window.requestAnimationFrame(() => {
    if (state.lightbox) els.lightbox.classList.add('is-open');
  });
  els.lightboxClose.focus();
  state.lightboxTrap = createFocusTrap(els.lightbox, closeLightbox);
  state.lightboxArrowHandler = (event) => {
    if (!state.lightbox) return;
    if (event.key === 'ArrowLeft') {
      event.preventDefault();
      lightboxStep(-1);
    } else if (event.key === 'ArrowRight') {
      event.preventDefault();
      lightboxStep(1);
    }
  };
  document.addEventListener('keydown', state.lightboxArrowHandler);
}

function renderLightbox() {
  if (!state.lightbox) return;
  const { card, index } = state.lightbox;
  const media = card.media[index];
  const count = card.media.length;

  // 媒体加载失败：先把 missing 落到 media 对象上再重绘，
  // 否则重绘会重新创建同一个坏 src，形成无限重建循环。
  const markMissing = () => {
    if (media.missing) return;
    media.missing = true;
    if (state.lightbox && state.lightbox.card === card) renderLightbox();
  };

  els.lightboxCounter.textContent = `${index + 1} / ${count}`;
  els.lightboxPrev.disabled = count <= 1;
  els.lightboxNext.disabled = count <= 1;
  els.lightboxStage.replaceChildren();

  const kind = media.t || 'photo';
  const missing = media.missing || (!media.src && !media.poster);
  let stageNode = null;

  if (els.lightboxDownload) {
    if (!missing && media.src) {
      els.lightboxDownload.hidden = false;
      els.lightboxDownload.href = media.src;
    } else {
      els.lightboxDownload.hidden = true;
      els.lightboxDownload.removeAttribute('href');
    }
  }

  if (missing) {
    stageNode = h(
      'div',
      { class: 'lightbox-missing' },
      icon(kind === 'photo' ? 'image' : 'film'),
      h('strong', {
        text:
          kind === 'video'
            ? '视频文件缺失'
            : kind === 'animated_gif'
              ? '动图文件缺失'
              : '图片文件缺失',
      }),
      h('small', { text: '原始文件未随归档保存' })
    );
  } else if (kind === 'photo') {
    stageNode = h('img', {
      src: media.src || media.poster,
      alt: mediaAlt(card, media, index),
      draggable: 'false',
    });
    setupImageZoom(stageNode);
    stageNode.addEventListener('error', markMissing, { once: true });
  } else if (kind === 'animated_gif') {
    if (media.src && /\.(mp4|webm|m4v)(\?|$)/i.test(media.src)) {
      stageNode = h('video', {
        src: media.src,
        poster: media.poster || '',
        autoplay: true,
        muted: true,
        loop: true,
        playsinline: true,
        controls: false,
      });
      attachVideoReporter(stageNode, card, media);
      stageNode.addEventListener('error', markMissing, { once: true });
    } else {
      stageNode = h('img', {
        src: media.src || media.poster,
        alt: mediaAlt(card, media, index),
      });
      stageNode.addEventListener('error', markMissing, { once: true });
    }
  } else {
    stageNode = h('video', {
      src: media.src,
      poster: media.poster || '',
      controls: true,
      autoplay: true,
      muted: true,
      playsinline: true,
      preload: 'metadata',
    });
    applyResume(stageNode, media);
    attachVideoReporter(stageNode, card, media);
    stageNode.addEventListener('error', markMissing, { once: true });
  }

  els.lightboxStage.append(stageNode);
  state.lightbox.video =
    stageNode && stageNode.tagName === 'VIDEO' ? stageNode : null;

  const caption = h('div');
  caption.append(
    h('strong', { text: card.handle ? `@${card.handle}` : card.name || '媒体' })
  );
  const body = (card.text || '').trim();
  if (body) caption.append(h('p', { text: cut(body, 180) }));
  const metrics = archiveMetricsLine(card);
  if (metrics) caption.append(metrics);
  els.lightboxCaption.replaceChildren(caption);
  els.lightboxCaption.hidden = !body && !card.handle && !metrics;
}

function setupImageZoom(image) {
  let zoom = 1;
  let panX = 0;
  let panY = 0;
  let drag = null;

  const apply = (immediate = false) => {
    image.style.transition = immediate ? 'none' : '';
    image.style.transform = `translate3d(${panX}px, ${panY}px, 0) scale(${zoom})`;
    image.classList.toggle('is-zoomed', zoom > 1);
  };

  image.title = '双击缩放 · 滚轮缩放 · 放大后拖动平移';
  image.addEventListener('dblclick', (event) => {
    event.preventDefault();
    zoom = zoom > 1 ? 1 : 2.5;
    panX = 0;
    panY = 0;
    apply();
  });

  image.addEventListener(
    'wheel',
    (event) => {
      event.preventDefault();
      const next = Math.max(1, Math.min(4, zoom * (event.deltaY < 0 ? 1.15 : 0.87)));
      if (next === 1) {
        panX = 0;
        panY = 0;
      }
      zoom = next;
      apply();
    },
    { passive: false }
  );

  image.addEventListener('pointerdown', (event) => {
    if (zoom <= 1) return;
    event.preventDefault();
    image.setPointerCapture(event.pointerId);
    drag = { x: event.clientX - panX, y: event.clientY - panY };
    image.classList.add('is-panning');
  });
  image.addEventListener('pointermove', (event) => {
    if (!drag) return;
    panX = event.clientX - drag.x;
    panY = event.clientY - drag.y;
    apply(true);
  });
  const endDrag = () => {
    if (!drag) return;
    drag = null;
    image.classList.remove('is-panning');
    apply();
  };
  image.addEventListener('pointerup', endDrag);
  image.addEventListener('pointercancel', endDrag);
}

function lightboxStep(delta) {
  if (!state.lightbox) return;
  const { card, index } = state.lightbox;
  const next = (index + delta + card.media.length) % card.media.length;
  if (state.lightbox.video) {
    state.lightbox.video.pause();
    state.lightbox.video = null;
  }
  state.lightbox.index = next;
  renderLightbox();
}

function closeLightbox() {
  if (!state.lightbox) return;
  if (state.lightbox.video) state.lightbox.video.pause();
  const trigger = state.lightbox.trigger;
  state.lightbox = null;
  els.lightbox.classList.remove('is-open');
  const finish = () => {
    state.lightboxCloseTimer = null;
    if (state.lightbox) return;
    els.lightbox.hidden = true;
    els.lightboxStage.replaceChildren();
    els.lightboxCaption.replaceChildren();
  };
  if (state.lightboxCloseTimer) window.clearTimeout(state.lightboxCloseTimer);
  if (motionReduced()) finish();
  else state.lightboxCloseTimer = window.setTimeout(finish, 200);
  document.body.classList.remove('is-locked');
  if (state.lightboxTrap) {
    state.lightboxTrap();
    state.lightboxTrap = null;
  }
  if (state.lightboxArrowHandler) {
    document.removeEventListener('keydown', state.lightboxArrowHandler);
    state.lightboxArrowHandler = null;
  }
  if (trigger && typeof trigger.focus === 'function') trigger.focus();
}

/* =========================================================
   统计
   ========================================================= */

function emptyStatsData() {
  return {
    totals: {
      seen_tweets: 0,
      plays: 0,
      watched_ms: 0,
      sessions: 0,
      seen_today: 0,
      favorites: 0,
      last_seen_at: null,
    },
    daily: [],
    top_media: [],
    recent: [],
  };
}

async function loadStats() {
  const generation = ++state.statsGeneration;
  if (DEMO === 'loading') {
    els.headMeta.textContent = '读取统计…';
    return;
  }
  try {
    const [data, meta] = await Promise.all([
      getJSON(`/api/stats?days=${state.statsDays}`),
      loadMeta().catch(() => null),
    ]);
    if (generation !== state.statsGeneration) return;
    if (meta) state.meta = meta;
    renderStats(data, meta);
  } catch (error) {
    if (generation === state.statsGeneration) renderStatsError(error);
  }
}

function formatBytes(value) {
  if (value == null) return '—';
  const number = Number(value);
  if (!Number.isFinite(number)) return '—';
  if (number < 1024) return `${Math.trunc(number)} B`;
  const units = ['KB', 'MB', 'GB', 'TB', 'PB'];
  let size = number;
  let index = -1;
  while (size >= 1024 && index < units.length - 1) {
    size /= 1024;
    index += 1;
  }
  return `${size.toFixed(size < 100 ? 1 : 0)} ${units[index]}`;
}

function storageSection(meta) {
  const storage = meta && meta.storage;
  if (!storage) return null;
  const store = (meta && meta.store) || {};

  const section = h('section', { class: 'stats-section storage-section' });
  section.append(
    h(
      'div',
      { class: 'stats-section-head' },
      h('h2', { text: '空间占用' }),
      h('span', { text: '本机归档磁盘' })
    )
  );

  const rows = [
    ['数据库', 'db_bytes'],
    ['媒体文件', 'media_bytes'],
    ['头像', 'avatars_bytes'],
    ['视频封面', 'posters_bytes'],
    ['临时残留', 'tmp_bytes'],
    ['投影副本', 'projection_bytes'],
    ['查看器媒体', 'viewer_media_bytes'],
  ];
  const list = h('dl', { class: 'storage-list' });
  for (const [label, key] of rows) {
    const raw = storage[key];
    const item = h('div', { class: 'storage-row' });
    item.append(h('dt', { text: label }));
    item.append(
      h('dd', {
        text: formatBytes(raw),
        title: raw == null ? '' : `${num(raw)} 字节`,
      })
    );
    list.append(item);
  }
  section.append(list);
  section.append(
    h('p', {
      class: 'storage-note',
      text:
        '数字来自最近一次导出的 meta.json，维护操作后需要重新导出才会刷新；' +
        '媒体与投影多为硬链接，各项相加不等于实际占盘。',
    })
  );

  const status = h('p', { class: 'storage-status', role: 'status' });
  const clean = h('button', {
    type: 'button',
    class: 'storage-btn',
    text: '清理临时残留',
    onclick: () => cleanStoreTmp(clean, status),
  });
  const gc = h('button', {
    type: 'button',
    class: 'storage-btn is-danger',
    text: '回收无效旧档',
    onclick: () => gcStore(gc, status),
  });
  if (!store.clean_tmp) {
    clean.disabled = true;
    clean.title = '服务端没有找到 store/.tmp（用 --store 指定归档 store）';
  }
  if (!store.gc) {
    gc.disabled = true;
    gc.title = '服务端没有找到 tools/store_v3.py，无法执行回收';
  }

  const actions = h(
    'div',
    { class: 'storage-actions' },
    h(
      'div',
      { class: 'storage-actions-copy' },
      h('strong', { text: '安全维护' }),
      h('small', {
        text: '只清理 store/.tmp 的 *.part 与「无人引用的无效旧档」；不会删除正本媒体或数据库。',
      })
    ),
    h('div', { class: 'storage-buttons' }, clean, gc)
  );
  section.append(actions, status);
  return section;
}

function setStorageBusy(button, busy) {
  if (!button) return;
  button.disabled = busy;
  button.classList.toggle('is-busy', busy);
  if (busy) {
    button.dataset.idleText = button.textContent;
    button.textContent = '处理中…';
  } else if (button.dataset.idleText) {
    button.textContent = button.dataset.idleText;
    delete button.dataset.idleText;
  }
}

async function cleanStoreTmp(button, status) {
  const confirmed = window.confirm(
    '只删除 store/.tmp 下中断下载留下的 *.part 临时文件，不会碰媒体原文件、数据库或投影。\n\n继续清理？'
  );
  if (!confirmed) return;
  setStorageBusy(button, true);
  if (status) status.textContent = '正在清理 store/.tmp…';
  try {
    const result = await postJSON('/api/cache/clean', { target: 'tmp' });
    const message = `已清理 ${num(result.removed)} 个临时文件，释放 ${formatBytes(
      result.freed_bytes
    )}`;
    if (status) status.textContent = message;
    showToast(message);
  } catch (error) {
    const message = `清理失败：${error && error.message ? error.message : '未知错误'}`;
    if (status) status.textContent = message;
    showToast(message);
  } finally {
    setStorageBusy(button, false);
  }
}

async function gcStore(button, status) {
  const confirmed = window.confirm(
    '回收无效旧档会运行 store_v3.py --gc，删除两类东西：\n' +
      '· 已被更高画质档替换、且不再被引用的旧档记录；\n' +
      '· 没有任何引用指向的孤立媒体文件。\n\n' +
      '删除后不可恢复；仍被引用的正本媒体和数据库不会被删除。需要先拿到 store 锁。\n\n确定继续？'
  );
  if (!confirmed) return;
  setStorageBusy(button, true);
  if (status) status.textContent = '正在执行 store_v3.py --gc…';
  try {
    const result = await postJSON('/api/store/gc', {});
    const freed =
      result.reclaimed_bytes == null ? '0 B' : formatBytes(result.reclaimed_bytes);
    const message = result.dry_run
      ? `演练完成：可回收 ${result.reclaimable || freed}`
      : `回收完成，释放 ${freed}`;
    if (status) status.textContent = message;
    showToast(message);
  } catch (error) {
    const message = `回收失败：${error && error.message ? error.message : '未知错误'}`;
    if (status) status.textContent = message;
    showToast(message);
  } finally {
    setStorageBusy(button, false);
  }
}

function renderStats(data, meta) {
  const totals = data.totals || {};
  const body = h('div', { class: 'stats-body' });

  const range = h('div', { class: 'stats-range', role: 'group', 'aria-label': '统计范围' });
  for (const days of [7, 14, 30, 60]) {
    const button = h('button', {
      type: 'button',
      class: `stats-range-btn${state.statsDays === days ? ' is-active' : ''}`,
      text: `${days} 天`,
      'aria-pressed': state.statsDays === days ? 'true' : 'false',
      onclick: () => {
        if (state.statsDays === days) return;
        state.statsDays = days;
        loadStats();
      },
    });
    range.append(button);
  }
  const reset = h('button', {
    type: 'button',
    class: 'stats-reset-link',
    text: '清空统计',
    onclick: resetState,
  });
  range.append(reset);
  body.append(range);

  const overview = h('div', { class: 'stats-overview' });
  const seen = Number(totals.seen_tweets) || 0;
  const plays = Number(totals.plays) || 0;
  const favorites = Number(totals.favorites) || 0;
  const watched = watchedParts(totals.watched_ms);

  overview.append(
    h(
      'div',
      { class: 'stat-cell' },
      h('div', { class: 'stat-label', text: '看过推文' }),
      h('div', { class: 'stat-value' }, num(seen), h('small', { text: '条' }))
    ),
    h(
      'div',
      { class: 'stat-cell' },
      h('div', { class: 'stat-label', text: '播放次数' }),
      h('div', { class: 'stat-value' }, num(plays), h('small', { text: '次' }))
    ),
    h(
      'div',
      { class: 'stat-cell' },
      h('div', { class: 'stat-label', text: '观看时长' }),
      h(
        'div',
        { class: 'stat-value' },
        watched.value,
        h('small', { text: watched.unit })
      )
    ),
    h(
      'div',
      { class: 'stat-cell' },
      h('div', { class: 'stat-label', text: '收藏' }),
      h('div', { class: 'stat-value' }, num(favorites), h('small', { text: '条' }))
    )
  );
  body.append(overview);

  const captionParts = [
    `今天看过 ${num(totals.seen_today)} 条`,
    `共 ${num(totals.sessions)} 次会话`,
  ];
  if (totals.last_seen_at) {
    captionParts.push(`最近 ${relativeTime(totals.last_seen_at)}`);
  }
  body.append(h('p', { class: 'stats-caption', text: captionParts.join(' · ') }));

  if (seen === 0 && plays === 0 && favorites === 0) {
    body.append(statsEmpty());
    const storage = storageSection(meta);
    if (storage) body.append(storage);
    els.statsView.replaceChildren(body);
    els.headMeta.textContent = '观看、播放与收藏';
    return;
  }

  const days = buildDays(data.daily || [], state.statsDays);
  const maxValue = Math.max(1, ...days.map((day) => Math.max(day.seen, day.plays)));
  const hasChart = days.some((day) => day.seen > 0 || day.plays > 0);

  const chartSection = h(
    'section',
    { class: 'stats-section' },
    h(
      'div',
      { class: 'stats-section-head' },
      h('h2', { text: `近 ${state.statsDays} 天` }),
      h('span', { text: '看过 / 播放' })
    )
  );

  if (hasChart) {
    const chart = h('div', { class: 'stats-chart' });
    chart.style.gridTemplateColumns = `repeat(${days.length}, minmax(0, 1fr))`;
    days.forEach((day, index) => {
      const bars = h('div', { class: 'chart-bars' });
      const seenBar = h('span', { class: 'chart-bar' });
      const playBar = h('span', { class: 'chart-bar plays' });
      seenBar.style.height = `${Math.max(0, (day.seen / maxValue) * 100)}%`;
      playBar.style.height = `${Math.max(0, (day.plays / maxValue) * 100)}%`;
      bars.append(seenBar, playBar);
      const label = index % 3 === 0 || index === days.length - 1 ? day.label : '';
      chart.append(
        h(
          'div',
          {
            class: 'chart-day',
            title: `${day.key} · 看过 ${day.seen} · 播放 ${day.plays}`,
          },
          bars,
          h('span', { class: 'chart-label', text: label })
        )
      );
    });
    chartSection.append(chart);
    chartSection.append(
      h(
        'div',
        { class: 'chart-legend' },
        h('span', {}, h('i'), '看过'),
        h('span', {}, h('i', { class: 'plays' }), '播放')
      )
    );
  } else {
    chartSection.append(
      h('div', { class: 'chart-empty', text: '还没有按天的观看记录' })
    );
  }
  body.append(chartSection);

  const top = (data.top_media || []).slice(0, 6);
  const topSection = h(
    'section',
    { class: 'stats-section' },
    h(
      'div',
      { class: 'stats-section-head' },
      h('h2', { text: '最常看' }),
      h('span', { text: top.length ? `${top.length} 个媒体` : '' })
    )
  );
  if (top.length) {
    const list = h('div', { class: 'stats-media-list' });
    top.forEach((item) => list.append(renderTopMedia(item)));
    topSection.append(list);
  } else {
    topSection.append(
      h('p', { class: 'stats-empty-inline', text: '还没有播放记录。' })
    );
  }
  body.append(topSection);

  const recent = (data.recent || []).slice(0, 6);
  const recentSection = h(
    'section',
    { class: 'stats-section' },
    h(
      'div',
      { class: 'stats-section-head' },
      h('h2', { text: '最近看过' }),
      h('span', { text: recent.length ? `${recent.length} 条` : '' })
    )
  );
  if (recent.length) {
    const list = h('div', { class: 'recent-list' });
    recent.forEach((item) => list.append(renderRecent(item)));
    recentSection.append(list);
  } else {
    recentSection.append(
      h('p', { class: 'stats-empty-inline', text: '还没有最近记录。' })
    );
  }
  body.append(recentSection);

  const storage = storageSection(meta);
  if (storage) body.append(storage);

  body.append(
    h('p', {
      class: 'stats-note',
      text: '统计只写入本机状态库；收藏独立保存，清空统计不会删除收藏。',
    })
  );

  els.statsView.replaceChildren(body);
  els.headMeta.textContent = '观看、播放与收藏';
}

function statsEmpty() {
  const action = h('a', {
    class: 'state-action',
    href: '/',
    text: '去时间线',
  });
  const wrap = h('div', { class: 'stats-empty' });
  wrap.append(
    emptyMark(),
    h('p', { class: 'state-kicker', text: '还没有记录' }),
    h('h2', { text: '先去看一会儿' }),
    h('p', {
      text: '卡片在视口里停留 1.5 秒会记一次已读；视频播放超过 1 秒会记一次播放。',
    }),
    action
  );
  return wrap;
}

function renderStatsError(error) {
  const retry = h('button', {
    type: 'button',
    class: 'state-action',
    text: '重新加载',
    onclick: () => {
      els.statsView.innerHTML = initialStatsHtml;
      loadStats();
    },
  });
  els.statsView.replaceChildren(
    stateBox(
      '读取失败',
      '统计没有读出来',
      `本地服务没有返回可用的数据。${error && error.message ? `（${error.message}）` : ''}`,
      retry
    )
  );
  els.headMeta.textContent = '读取失败';
}

function buildDays(daily, count = 14) {
  const map = new Map((daily || []).map((item) => [item.d, item]));
  const days = [];
  const now = new Date();
  for (let i = count - 1; i >= 0; i -= 1) {
    const date = new Date(now.getFullYear(), now.getMonth(), now.getDate() - i, 12);
    const key = dateKey(date);
    const item = map.get(key) || {};
    days.push({
      key,
      label: `${date.getMonth() + 1}/${date.getDate()}`,
      seen: Number(item.seen) || 0,
      plays: Number(item.plays) || 0,
    });
  }
  return days;
}

function renderTopMedia(item) {
  const kind = item.kind || 'video';
  const src = item.poster || item.src || '';
  const hasMedia = Boolean(item.poster || item.src);
  const thumb = h('span', { class: 'stat-media-thumb' });
  if (src) {
    const image = h('img', {
      src,
      alt: '',
      loading: 'lazy',
      decoding: 'async',
    });
    image.addEventListener(
      'error',
      () => thumb.replaceChildren(icon(kind === 'photo' ? 'image' : 'film')),
      { once: true }
    );
    thumb.append(image);
  } else {
    thumb.append(icon(kind === 'photo' ? 'image' : 'film'));
  }

  const copy = h(
    'span',
    { class: 'stat-media-main' },
    h('span', {
      class: 'stat-media-title',
      text: cut(item.text || item.handle || '媒体', 48),
    }),
    h('span', {
      class: 'stat-media-meta',
      text: `${item.handle ? `@${item.handle}` : '未知作者'} · ${watchedShort(item.watched_ms)}`,
    })
  );
  const plays = h('span', {
    class: 'stat-media-plays',
    text: `${num(item.plays)} 次`,
  });

  const node = hasMedia
    ? h('button', { type: 'button', class: 'stat-media' })
    : h('div', { class: 'stat-media' });
  node.append(thumb, copy, plays);

  if (hasMedia) {
    node.addEventListener('click', () => {
      const media = {
        key: item.media_key,
        t: kind,
        src: item.src || null,
        poster: item.poster || null,
        ms: item.duration_ms || null,
        missing: !item.src && !item.poster,
      };
      const card = {
        id: item.tweet_id,
        handle: item.handle,
        name: item.name,
        text: item.text || '',
        media: [media],
      };
      openLightbox(card, 0, node);
    });
  }
  return node;
}

function renderRecent(item) {
  return h(
    'div',
    { class: 'recent-row' },
    fillAvatar(
      h('span', { class: 'avatar recent-avatar', 'aria-hidden': 'true' }),
      item.avatar,
      item.name,
      item.handle
    ),
    h(
      'div',
      { class: 'recent-main' },
      h(
        'div',
        { class: 'recent-head' },
        h('span', {
          class: 'recent-name',
          text: item.name || item.handle || '未知作者',
        }),
        item.handle
          ? h('span', { class: 'recent-handle', text: `@${item.handle}` })
          : null,
        h('span', {
          class: 'recent-time',
          text: relativeTime(item.last_seen_at),
        })
      ),
      h('div', { class: 'recent-text', text: cut(item.text || '', 90) })
    )
  );
}

/* =========================================================
   全局事件
   ========================================================= */

function bindGlobalEvents() {
  const bindArrowNav = (container, attr) => {
    container.addEventListener('keydown', (event) => {
      if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
      const nodes = Array.from(container.querySelectorAll(`[${attr}]`));
      if (!nodes.length) return;
      let index = nodes.indexOf(document.activeElement);
      if (index < 0) index = nodes.findIndex((node) => node.classList.contains('is-active'));
      if (event.key === 'Home') index = 0;
      else if (event.key === 'End') index = nodes.length - 1;
      else if (event.key === 'ArrowLeft') index = (index - 1 + nodes.length) % nodes.length;
      else index = (index + 1) % nodes.length;
      event.preventDefault();
      nodes[index].focus();
    });
  };

  // 媒体类型 tab：全部 / 图片 / 视频 / 文字
  if (els.tabs) {
    els.tabs.addEventListener('click', (event) => {
      const tab = event.target.closest('[data-media-type]');
      if (!tab) return;
      const value = tab.dataset.mediaType;
      if (!MEDIA_TYPES.includes(value) || value === state.mediaType) return;
      state.mediaType = value;
      updateFilterUi();
      syncUrl();
      reloadFeed();
    });
    bindArrowNav(els.tabs, 'data-media-type');
  }

  if (els.mediaTabs) {
    els.mediaTabs.addEventListener('click', (event) => {
      const tab = event.target.closest('[data-media-filter]');
      if (!tab) return;
      const filter = tab.dataset.mediaFilter;
      if (!MEDIA_FILTERS.includes(filter) || filter === state.media.filter) return;
      state.media.filter = filter;
      updateMediaTabs();
      syncUrl();
      reloadMedia();
    });

    els.mediaTabs.addEventListener('keydown', (event) => {
      if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
      const tabs = Array.from(
        els.mediaTabs.querySelectorAll('[data-media-filter]')
      );
      let index = tabs.indexOf(document.activeElement);
      if (index < 0) {
        index = tabs.findIndex(
          (tab) => tab.dataset.mediaFilter === state.media.filter
        );
      }
      if (event.key === 'Home') index = 0;
      else if (event.key === 'End') index = tabs.length - 1;
      else if (event.key === 'ArrowLeft') index = (index - 1 + tabs.length) % tabs.length;
      else index = (index + 1) % tabs.length;
      event.preventDefault();
      const next = tabs[index];
      if (!next) return;
      next.focus();
      if (next.dataset.mediaFilter !== state.media.filter) next.click();
    });
  }

  if (els.accountSelect) {
    els.accountSelect.addEventListener('change', () => {
      const value = els.accountSelect.value || '';
      if (value === state.account) return;
      state.account = value;
      updateAccountUi();
      syncUrl();
      reloadFeed();
    });
  }

  document.addEventListener('click', (event) => {
    if (!state.moreOpenId) return;
    if (event.target.closest('.tweet-menu')) return;
    if (event.target.closest('[data-more]')) return;
    closeTweetMenus(false);
  });

  document.addEventListener('keydown', handleMenuKeydown);
  document.addEventListener('keydown', handleGlobalShortcut);

  if (els.filterBtn && els.filterPanel) {
    els.filterBtn.addEventListener('click', () => {
      setFilterPanel(els.filterPanel.hidden);
    });
    // 面板里的来源 / 阅读状态 / 排序只改暂存态，等「应用」才生效。
    els.filterPanel.addEventListener('click', (event) => {
      const chip = event.target.closest(
        '[data-source], [data-read-state], [data-sort]'
      );
      if (!chip || !els.filterPanel.contains(chip)) return;
      if (chip.dataset.source) filterDraft.source = chip.dataset.source;
      else if (chip.dataset.readState) filterDraft.readState = chip.dataset.readState;
      else if (chip.dataset.sort) filterDraft.sort = chip.dataset.sort;
      renderFilterDraft();
    });
    // 三组胶囊沿用左右方向键在组内移动焦点。
    els.filterPanel.querySelectorAll('.filter-chips').forEach((group) => {
      const attr = group.querySelector('[data-source]')
        ? 'data-source'
        : group.querySelector('[data-read-state]')
          ? 'data-read-state'
          : 'data-sort';
      bindArrowNav(group, attr);
    });
    els.filterApply.addEventListener('click', applyAdvancedFilters);
    els.filterClear.addEventListener('click', clearAdvancedFilters);
    els.filterPanel.addEventListener('keydown', (event) => {
      if (event.key !== 'Escape') return;
      event.preventDefault();
      event.stopPropagation();
      setFilterPanel(false);
      els.filterBtn.focus();
    });
    // 点面板外面收起面板；未应用的暂存选择下次打开时会被 state 覆盖。
    document.addEventListener('click', (event) => {
      if (els.filterPanel.hidden) return;
      if (els.filterPanel.contains(event.target)) return;
      if (els.filterBtn.contains(event.target)) return;
      setFilterPanel(false);
    });
  }

  els.searchInputs.forEach((input) => {
    input.addEventListener('focus', () => {
      renderSearchHistory(input);
    });

    input.addEventListener('blur', () => {
      window.setTimeout(() => {
        const panel = searchHistoryPanelFor(input);
        if (panel && panel.contains(document.activeElement)) return;
        hideSearchHistory();
      }, 120);
    });

    input.addEventListener('input', () => {
      const value = input.value;
      els.searchInputs.forEach((other) => {
        if (other !== input) other.value = value;
      });
      updateSearchClear();
      if (value.trim()) hideSearchHistory();
      else renderSearchHistory(input);
      if (state.author && VIEW !== 'author') {
        state.author = '';
        updateAuthorContext();
      }
      if (state.searchTimer) window.clearTimeout(state.searchTimer);
      state.searchTimer = window.setTimeout(() => {
        const q = input.value.trim();
        if (q === state.q) return;
        state.q = q;
        if (q) recordSearch(q);
        syncUrl();
        reloadCurrent();
      }, 300);
    });

    input.addEventListener('keydown', (event) => {
      if (event.key === 'ArrowDown' && !input.value.trim()) {
        const panel = searchHistoryPanelFor(input);
        const first = panel && panel.querySelector('.search-history-item');
        if (first) {
          event.preventDefault();
          first.focus();
          return;
        }
      }
      if (event.key === 'Escape') {
        event.preventDefault();
        hideSearchHistory();
        clearSearch();
        input.blur();
      }
    });
  });

  els.searchClears.forEach((button) => {
    button.addEventListener('click', clearSearch);
  });

  if (els.clearAuthor) els.clearAuthor.addEventListener('click', clearAuthor);

  els.feed.addEventListener('click', (event) => {
    const more = event.target.closest('button[data-more]');
    if (more) {
      event.preventDefault();
      event.stopPropagation();
      const article = more.closest('article.tweet');
      if (!article) return;
      if (article.dataset.tweetId === state.moreOpenId) closeTweetMenus(true);
      else openTweetMenu(article, more);
      return;
    }

    const menuAction = event.target.closest('[data-menu-action]');
    if (menuAction) {
      event.preventDefault();
      event.stopPropagation();
      const article = menuAction.closest('article.tweet');
      const card = state.cards.get(menuAction.dataset.tweetId);
      closeTweetMenus(false);
      if (article && card) {
        handleTweetMenu(card, menuAction.dataset.menuAction, article);
      }
      return;
    }

    const noteEdit = event.target.closest('[data-note-edit]');
    if (noteEdit) {
      event.preventDefault();
      event.stopPropagation();
      const card = state.cards.get(noteEdit.dataset.noteEdit);
      if (card) openNote(card, noteEdit);
      return;
    }

    const actionButton = event.target.closest('button[data-action]');
    if (actionButton) {
      const action = actionButton.dataset.action;
      if (action === 'bookmark') {
        event.preventDefault();
        event.stopPropagation();
        toggleFavorite(actionButton.dataset.tweetId, actionButton);
        return;
      }
      if (action === 'reply') {
        event.preventDefault();
        event.stopPropagation();
        const card = state.cards.get(actionButton.dataset.tweetId);
        if (card) openNote(card, actionButton);
        return;
      }
      if (action === 'repost' || action === 'like') {
        event.preventDefault();
        event.stopPropagation();
        const card = state.cards.get(actionButton.dataset.tweetId);
        if (card) toggleLocalMark(card, action, actionButton);
        return;
      }
      if (action === 'share') {
        event.preventDefault();
        event.stopPropagation();
        const card = state.cards.get(actionButton.dataset.tweetId);
        if (card) shareTweet(card);
        return;
      }
    }

    const open = event.target.closest('.media-open');
    if (open) {
      const card = state.cards.get(open.dataset.tweetId);
      const index = Number.parseInt(open.dataset.mediaIndex, 10);
      if (card && Number.isFinite(index)) {
        event.preventDefault();
        openLightbox(card, index, open);
      }
      return;
    }

    const avatar = event.target.closest('button.avatar[data-author]');
    if (avatar) {
      event.preventDefault();
      event.stopPropagation();
      setAuthor(avatar.dataset.author);
    }
  });

  if (els.mediaGrid) {
    els.mediaGrid.addEventListener('click', (event) => {
      const tile = event.target.closest('button[data-media-key]');
      if (!tile) return;
      const item = state.media.items.get(tile.dataset.mediaKey);
      if (!item) return;
      event.preventDefault();
      openMediaItem(item, tile);
    });
  }

  document.querySelectorAll('[data-open-settings]').forEach((trigger) => {
    trigger.addEventListener('click', () => openSettings(trigger));
  });

  document.querySelectorAll('[data-close-settings]').forEach((button) => {
    button.addEventListener('click', closeSettings);
  });

  if (els.settingsBackdrop) {
    els.settingsBackdrop.addEventListener('click', closeSettings);
  }

  els.themeRows.forEach((row) => {
    row.addEventListener('click', () => {
      const theme = row.dataset.themeChoice;
      if (typeof window.timelineTheme === 'function') {
        window.timelineTheme(theme);
      } else {
        document.documentElement.setAttribute('data-theme', theme);
      }
      const url = new URL(window.location.href);
      url.searchParams.delete('theme');
      window.history.replaceState(null, '', `${url.pathname}${url.search}`);
      updateThemeRows();
    });

    row.addEventListener('keydown', (event) => {
      if (!['ArrowDown', 'ArrowRight', 'ArrowUp', 'ArrowLeft', 'Home', 'End'].includes(event.key)) {
        return;
      }
      event.preventDefault();
      const current = els.themeRows.indexOf(row);
      const target =
        event.key === 'Home'
          ? 0
          : event.key === 'End'
            ? els.themeRows.length - 1
            : event.key === 'ArrowDown' || event.key === 'ArrowRight'
              ? (current + 1) % els.themeRows.length
              : (current - 1 + els.themeRows.length) % els.themeRows.length;
      const next = els.themeRows[target];
      if (!next) return;
      next.focus();
      next.click();
    });
  });

  document.addEventListener('timeline:theme', updateThemeRows);

  if (els.noteBackdrop) {
    els.noteBackdrop.addEventListener('click', closeNote);
  }
  els.noteCloses.forEach((button) => {
    button.addEventListener('click', closeNote);
  });
  if (els.noteInput) {
    els.noteInput.addEventListener('input', updateNoteCount);
    els.noteInput.addEventListener('keydown', (event) => {
      if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') {
        event.preventDefault();
        saveNote();
      }
    });
  }
  if (els.noteSave) els.noteSave.addEventListener('click', saveNote);
  if (els.noteDelete) els.noteDelete.addEventListener('click', deleteNote);

  if (els.autoplaySwitch) {
    els.autoplaySwitch.addEventListener('click', () => {
      state.autoplay = !state.autoplay;
      writeSetting('timeline:autoplay', state.autoplay);
      updateSettingsUi();
      refreshAutoplay();
    });
  }

  if (els.motionSwitch) {
    els.motionSwitch.addEventListener('click', () => {
      state.reduceMotion = !state.reduceMotion;
      writeSetting('timeline:reduce-motion', state.reduceMotion);
      document.documentElement.classList.toggle('reduce-motion', state.reduceMotion);
      updateSettingsUi();
      if (state.reduceMotion) {
        document.querySelectorAll('video').forEach((video) => video.pause());
      }
      refreshAutoplay();
    });
  }

  if (els.stateExport) els.stateExport.addEventListener('click', exportState);
  if (els.stateReset) els.stateReset.addEventListener('click', resetState);
  if (els.markAllSeen) els.markAllSeen.addEventListener('click', markAllSeen);
  if (els.localExport) els.localExport.addEventListener('click', exportLocalData);
  if (els.localImportTrigger && els.localImport) {
    els.localImportTrigger.addEventListener('click', () => els.localImport.click());
    els.localImport.addEventListener('change', importLocalFile);
  }

  if (els.notesOpenBtn) {
    els.notesOpenBtn.addEventListener('click', () => openNotes(els.notesOpenBtn));
  }
  document.querySelectorAll('[data-close-notes]').forEach((button) => {
    button.addEventListener('click', closeNotes);
  });
  if (els.notesBackdrop) {
    els.notesBackdrop.addEventListener('click', closeNotes);
  }
  if (els.notesSearch) {
    els.notesSearch.addEventListener('input', () => {
      renderNotesList(els.notesSearch.value);
    });
  }
  if (els.notesSearchClear && els.notesSearch) {
    els.notesSearchClear.addEventListener('click', () => {
      els.notesSearch.value = '';
      renderNotesList('');
      els.notesSearch.focus();
    });
  }

  if (els.lightboxClose) els.lightboxClose.addEventListener('click', closeLightbox);
  if (els.lightboxPrev) {
    els.lightboxPrev.addEventListener('click', () => lightboxStep(-1));
  }
  if (els.lightboxNext) {
    els.lightboxNext.addEventListener('click', () => lightboxStep(1));
  }
  if (els.lightbox) {
    els.lightbox.addEventListener('click', (event) => {
      if (event.target === els.lightbox || event.target === els.lightboxStage) {
        closeLightbox();
      }
    });
  }

  document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
      document.querySelectorAll('video').forEach((video) => video.pause());
      flushSeen(true);
    }
  });

  window.addEventListener('pagehide', () => {
    flushSeen(true);
    for (const flush of state.videoFlush) flush();
  });
}

function showToast(message) {
  if (!els.toast) return;
  els.toast.textContent = message;
  if (state.toastTimer) window.clearTimeout(state.toastTimer);
  showPopover(els.toast);
  state.toastTimer = window.setTimeout(() => {
    state.toastTimer = null;
    hidePopover(els.toast);
  }, 2600);
}
