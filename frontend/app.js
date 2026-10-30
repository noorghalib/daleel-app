'use strict';

// Shared icons, formatting and route labels. Application data is supplied by the integration adapter.
const ICONS = {
  calendar:'<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M7 3v4m10-4v4M3 11h18m-13 4h2m4 0h2"/>',
  'trend-down':'<path d="m3 7 6 6 4-3 8 10m-6 0h6v-6"/>',
  grid:'<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
  arrows:'<path d="M4 7h15m-4-4 4 4-4 4M20 17H5m4-4-4 4 4 4"/>',
  book:'<path d="M12 5c-3-2-6-2-9-1v15c3-1 6-1 9 1 3-2 6-2 9-1V4c-3-1-6-1-9 1v15M6 8h3m6 0h3M6 12h3m6 0h3"/>',
  scan:'<path d="M8 3H5a2 2 0 0 0-2 2v3m13-5h3a2 2 0 0 1 2 2v3M3 16v3a2 2 0 0 0 2 2h3m13-5v3a2 2 0 0 1-2 2h-3M8 12l3 3 5-6"/>',
  folder:'<path d="M3 7V5a2 2 0 0 1 2-2h5l2 3h7a2 2 0 0 1 2 2v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7Z"/><path d="M3 9h18"/>',
  chart:'<path d="M4 3v17h17M8 15v-4m5 4V6m5 9V9"/>',
  cpu:'<rect x="6" y="6" width="12" height="12" rx="3"/><rect x="10" y="10" width="4" height="4" rx="1"/><path d="M9 3v3m6-3v3M9 18v3m6-3v3M3 9h3m-3 6h3m12-6h3m-3 6h3"/>',
  settings:'<path d="m9.5 3-.6 2.2-2 .9-2-.8-2.4 4.1 1.7 1.5v2.2l-1.7 1.5 2.4 4.1 2-.8 2 .9.6 2.2h5l.6-2.2 2-.9 2 .8 2.4-4.1-1.7-1.5v-2.2l1.7-1.5-2.4-4.1-2 .8-2-.9L14.5 3h-5Z"/><circle cx="12" cy="12" r="3"/>',
  shield:'<path d="m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6l8-3Z"/><path d="m8.5 12 2.5 2.5 4.5-5"/>',
  help:'<circle cx="12" cy="12" r="9"/><path d="M9.5 9a2.5 2.5 0 1 1 4 2c-1 .6-1.5 1.1-1.5 2.5M12 17h.01"/>',
  external:'<path d="M14 3h7v7m0-7-10 10M10 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-5"/>',
  building:'<path d="M4 21V5a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v16M17 9h4v12M2 21h20M8 7h1m3 0h1M8 11h1m3 0h1m-5 4h1m3 0h1M9 21v-3h3v3"/>',
  chevrons:'<path d="m9 8 3-3 3 3m-6 8 3 3 3-3"/>',
  home:'<path d="m3 10 9-7 9 7v10H3V10Zm6 10v-7h6v7"/>',
  search:'<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 4.5 4.5"/>',
  bell:'<path d="M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9M10 21h4"/>',
  menu:'<path d="M4 6h16M4 12h16M4 18h16"/>',
  plus:'<path d="M12 5v14M5 12h14"/>',
  upload:'<path d="M12 16V3m-5 5 5-5 5 5M4 14v6a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-6"/>',
  download:'<path d="M12 3v13m-5-5 5 5 5-5M4 16v5h16v-5"/>',
  'arrow-left':'<path d="M20 12H4m6-6-6 6 6 6"/>',
  'arrow-right':'<path d="M4 12h16m-6-6 6 6-6 6"/>',
  'chevron-left':'<path d="m15 6-6 6 6 6"/>',
  'chevron-right':'<path d="m9 6 6 6-6 6"/>',
  trend:'<path d="m3 17 6-6 4 3 8-10m-6 0h6v6"/>',
  check:'<path d="m5 12 4 4L19 6"/>',
  'check-circle':'<circle cx="12" cy="12" r="9"/><path d="m7.5 12 3 3 6-6"/>',
  flag:'<path d="M5 21V4m0 0c5-4 9 4 14 0v10c-5 4-9-4-14 0"/>',
  alert:'<path d="m12 3 10 18H2L12 3Z"/><path d="M12 9v5m0 3h.01"/>',
  clock:'<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  info:'<circle cx="12" cy="12" r="9"/><path d="M12 11v6m0-10h.01"/>',
  close:'<path d="m6 6 12 12M6 18 18 6"/>',
  file:'<path d="M14 2H5v20h14V7l-5-5Z"/><path d="M14 2v5h5M8 12h8m-8 4h6"/>',
  filter:'<path d="M3 5h18l-7 8v6l-4 2v-8L3 5Z"/>',
  layers:'<path d="m12 3 10 5-10 5L2 8l10-5Zm-9 9 9 5 9-5M3 16l9 5 9-5"/>',
  eye:'<path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/>',
  lock:'<rect x="5" y="10" width="14" height="11" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3m-4 5v2"/>',
};
const icon = name => `<svg class="icon" viewBox="0 0 24 24" aria-hidden="true">${ICONS[name] || ICONS.file}</svg>`;
const escapeHTML = value => String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
const number = value => new Intl.NumberFormat('ar-IQ').format(value);
const decimal = value => new Intl.NumberFormat('ar-IQ',{maximumFractionDigits:4}).format(value);
const titles = {overview:'نظرة عامة',transactions:'المعاملات المالية',ledger:'القيود المحاسبية',review:'قائمة المراجعة',files:'الملفات والتحليلات',reports:'التقارير',models:'نماذج الذكاء الاصطناعي',settings:'الإعدادات'};
const subtitles = {overview:'صورة أوضح لعملياتك المالية، وكل ما يحتاج إلى انتباهك.',transactions:'استعرض المعاملات، وافهم الإشارات، وراجع التفاصيل.',ledger:'اكتشف الأنماط غير المعتادة في قيودك المحاسبية.',review:'الأولوية للحالات التي تستحق نظرة أقرب.',files:'ملفاتك وتحليلاتك، مرتبة في مساحة واحدة.',reports:'حوّل نتائج المراجعة إلى تقارير واضحة وقابلة للمشاركة.',models:'تعرّف على المحركات التي تدعم التحليل في دليل.',settings:'خصّص حسابك ومساحة العمل حسب احتياجك.'};
