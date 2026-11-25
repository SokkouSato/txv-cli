/* ==========================================================================
 * theme.js —— 三套主题（dark / dim / light）
 *
 * 在 <head> 里同步引入，先写 <html data-theme>，避免刷新时闪错色。
 * 优先级：?theme= > localStorage(timeline:theme) > 系统偏好。
 * 设置面板通过 window.timelineTheme(name) 切换，并监听 timeline:theme 事件。
 * ========================================================================== */
(function () {
  'use strict';

  var KEY = 'timeline:theme';
  var THEMES = ['dark', 'dim', 'light'];

  function fromUrl() {
    try {
      var value = new URLSearchParams(window.location.search).get('theme');
      return THEMES.indexOf(value) >= 0 ? value : null;
    } catch (e) {
      return null;
    }
  }

  function read() {
    var urlTheme = fromUrl();
    if (urlTheme) {
      try { localStorage.setItem(KEY, urlTheme); } catch (e) { /* 忽略 */ }
      return urlTheme;
    }
    try {
      var saved = localStorage.getItem(KEY);
      if (saved && THEMES.indexOf(saved) >= 0) return saved;
    } catch (e) { /* localStorage 不可用 */ }
    try {
      if (window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches) {
        return 'light';
      }
    } catch (e) { /* 忽略 */ }
    return 'dark';
  }

  var current = read();
  var hasSaved = false;
  try { hasSaved = THEMES.indexOf(localStorage.getItem(KEY)) >= 0; } catch (e) { /* 忽略 */ }

  function apply(theme, announce) {
    current = theme;
    document.documentElement.setAttribute('data-theme', theme);
    try {
      document.documentElement.style.colorScheme = theme === 'light' ? 'light' : 'dark';
    } catch (e) { /* 忽略 */ }
    if (announce) {
      try {
        document.dispatchEvent(new CustomEvent('timeline:theme', { detail: { theme: theme } }));
      } catch (e) { /* 忽略 */ }
    }
  }

  function persist(theme) {
    try { localStorage.setItem(KEY, theme); } catch (e) { /* 忽略 */ }
    hasSaved = true;
  }

  apply(current, false);

  // 页面里的主题切换走设置抽屉（[data-theme-choice]）调用 window.timelineTheme，
  // 这里不再维护 <select id="themeSelect"> 的旧实现。

  window.timelineTheme = function (name) {
    if (THEMES.indexOf(name) < 0) return current;
    persist(name);
    apply(name, true);
    return current;
  };

  window.timelineThemeCurrent = function () {
    return current;
  };

  // 用户在系统层切换浅/深色时，如果从未手动选过主题，则跟随。
  try {
    var media = window.matchMedia('(prefers-color-scheme: light)');
    media.addEventListener('change', function (event) {
      if (hasSaved || fromUrl()) return;
      apply(event.matches ? 'light' : 'dark', true);
    });
  } catch (e) { /* 忽略 */ }
})();
