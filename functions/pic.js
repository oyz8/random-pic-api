// ========================================
// 兼容 EdgeOne Pages + Cloudflare Pages
// ========================================

const COUNT_URL_PATH = '/ri/count.json';
const IMAGE_BASE_PATH = '/ri';
const IMAGE_EXT = '.webp';
const FOLDERS = ['hd', 'hl', 'vd', 'vl'];

const COUNT_CACHE_TTL = 5 * 60 * 1000;
const COUNT_ERROR_CACHE_TTL = 30 * 1000;

const MOBILE_KEYWORDS = [
  'mobile', 'android', 'iphone', 'ipad', 'ipod', 'blackberry',
  'windows phone', 'opera mini', 'iemobile', 'mobile safari',
  'webos', 'kindle', 'silk', 'fennec', 'maemo', 'tablet',
];

// -------- 运行时配置（默认值） --------
let ALLOWED_DOMAINS = [];
let ALLOW_EMPTY_REQUEST = true; // 默认允许空 Referer/Origin

// -------- 缓存 --------
let countData = null;
let validArrays = null;
let countExpiresAt = 0;
let countLoading = null;

// ========================================
// 配置初始化（从环境变量读取）
// ========================================
function initConfig(env) {
  if (!env) return;

  // 域名白名单：环境变量 ALLOWED_DOMAINS 为逗号分隔字符串
  // 未配置时保持空数组，即不授权任何带 Origin/Referer 的域名
  if (env.ALLOWED_DOMAINS && typeof env.ALLOWED_DOMAINS === 'string') {
    ALLOWED_DOMAINS = env.ALLOWED_DOMAINS
      .split(',')
      .map((d) => d.trim())
      .filter(Boolean);
  }

  // 是否允许空 Referer/Origin
  // 未配置时使用默认值 true
  if (env.ALLOW_EMPTY_REQUEST !== undefined) {
    ALLOW_EMPTY_REQUEST =
      env.ALLOW_EMPTY_REQUEST === true ||
      env.ALLOW_EMPTY_REQUEST === 'true' ||
      env.ALLOW_EMPTY_REQUEST === '1';
  }
}

// ========================================
// 辅助函数
// ========================================
function extractDomain(urlString) {
  if (!urlString) return null;
  try {
    return new URL(urlString).hostname.toLowerCase();
  } catch {
    return null;
  }
}

function isDomainAllowed(domain) {
  if (!domain) return false;

  const lowerDomain = domain.toLowerCase();

  return ALLOWED_DOMAINS.some((allowed) => {
    const lowerAllowed = allowed.toLowerCase();
    return (
      lowerDomain === lowerAllowed ||
      lowerDomain.endsWith('.' + lowerAllowed)
    );
  });
}

function checkAntiLeech(request) {
  const referer = request.headers.get('Referer');
  const origin = request.headers.get('Origin');

  const refererDomain = extractDomain(referer);
  const originDomain = extractDomain(origin);

  if (originDomain && isDomainAllowed(originDomain)) {
    return { allowed: true, domain: originDomain, source: 'Origin' };
  }

  if (refererDomain && isDomainAllowed(refererDomain)) {
    return { allowed: true, domain: refererDomain, source: 'Referer' };
  }

  if (!origin && !referer && ALLOW_EMPTY_REQUEST) {
    return { allowed: true, domain: null, source: 'empty' };
  }

  if (origin || referer) {
    return {
      allowed: false,
      reason: `域名未授权 (Origin: ${originDomain || '无'}, Referer: ${refererDomain || '无'})`,
    };
  }

  return {
    allowed: false,
    reason: '私人API 禁止直接访问',
  };
}

function resolveCorsOrigin(request) {
  const origin = request.headers.get('Origin');

  if (origin && isDomainAllowed(extractDomain(origin))) {
    return origin;
  }

  if (!origin && ALLOW_EMPTY_REQUEST) {
    return '*';
  }

  return null;
}

function buildHeaders(corsOrigin, extra = {}) {
  const headers = new Headers(extra);

  if (corsOrigin) {
    headers.set('Access-Control-Allow-Origin', corsOrigin);

    if (corsOrigin !== '*') {
      headers.set('Vary', 'Origin');
    }
  }

  return headers;
}

function jsonResponse(body, status, corsOrigin, extraHeaders = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: buildHeaders(corsOrigin, {
      'Content-Type': 'application/json; charset=utf-8',
      ...extraHeaders,
    }),
  });
}

function textResponse(text, status, corsOrigin, extraHeaders = {}) {
  return new Response(text, {
    status,
    headers: buildHeaders(corsOrigin, {
      'Content-Type': 'text/plain; charset=utf-8',
      ...extraHeaders,
    }),
  });
}

// ========================================
// count.json 加载与缓存
// ========================================
async function loadCount(request) {
  const now = Date.now();

  if (countData && now < countExpiresAt) {
    return countData;
  }

  if (countLoading) {
    return countLoading;
  }

  countLoading = (async () => {
    try {
      const countUrl = new URL(COUNT_URL_PATH, request.url).toString();
      const resp = await fetch(countUrl);

      if (!resp.ok) {
        throw new Error(`count.json HTTP ${resp.status}`);
      }

      const data = await resp.json();
      const arrays = {};

      for (const folder of FOLDERS) {
        arrays[folder] = buildValidArray(data[folder]);
      }

      countData = data;
      validArrays = arrays;
      countExpiresAt = Date.now() + COUNT_CACHE_TTL;

      return data;
    } catch (e) {
      console.error('Failed to load count.json:', e);

      const emptyData = {
        hd: { max: 0, exclude: [] },
        hl: { max: 0, exclude: [] },
        vd: { max: 0, exclude: [] },
        vl: { max: 0, exclude: [] },
      };

      countData = emptyData;
      validArrays = { hd: [], hl: [], vd: [], vl: [] };
      countExpiresAt = Date.now() + COUNT_ERROR_CACHE_TTL;

      return emptyData;
    } finally {
      countLoading = null;
    }
  })();

  return countLoading;
}

function buildValidArray(config) {
  const max = Number(config?.max) || 0;

  if (max <= 0) return [];

  const rawExclude = Array.isArray(config?.exclude) ? config.exclude : [];
  const exclude = new Set(rawExclude.map(Number));
  const arr = [];

  for (let i = 1; i <= max; i++) {
    if (!exclude.has(i)) {
      arr.push(i);
    }
  }

  return arr;
}

// ========================================
// 设备判断与随机选择
// ========================================
function isMobileDevice(userAgent) {
  if (!userAgent) return false;

  const ua = userAgent.toLowerCase();
  return MOBILE_KEYWORDS.some((keyword) => ua.includes(keyword));
}

function randomFromTwoFolders(darkFolder, lightFolder) {
  const darkArr = validArrays?.[darkFolder] || [];
  const lightArr = validArrays?.[lightFolder] || [];
  const total = darkArr.length + lightArr.length;

  if (total === 0) return null;

  const idx = Math.floor(Math.random() * total);

  if (idx < darkArr.length) {
    return {
      folder: darkFolder,
      file: darkArr[idx],
      theme: 'dark',
    };
  }

  return {
    folder: lightFolder,
    file: lightArr[idx - darkArr.length],
    theme: 'light',
  };
}

// ========================================
// 核心请求处理
// ========================================
async function handleRequest(request, env) {
  initConfig(env);

  let corsOrigin = null;

  try {
    const url = new URL(request.url);

    // ---------- CORS 预检 ----------
    if (request.method === 'OPTIONS') {
      const origin = request.headers.get('Origin');
      const originDomain = extractDomain(origin);

      if (!origin || !isDomainAllowed(originDomain)) {
        return textResponse('CORS 未授权', 403, null);
      }

      return new Response(null, {
        status: 204,
        headers: {
          'Access-Control-Allow-Origin': origin,
          'Access-Control-Allow-Methods': 'GET, HEAD, OPTIONS',
          'Access-Control-Allow-Headers': 'Content-Type',
          'Access-Control-Max-Age': '86400',
          'Vary': 'Origin',
        },
      });
    }

    // ---------- 方法限制 ----------
    if (request.method !== 'GET' && request.method !== 'HEAD') {
      return textResponse('私人API 未授权', 405, null, {
        Allow: 'GET, HEAD, OPTIONS',
      });
    }

    // ---------- 防盗链 ----------
    const leechCheck = checkAntiLeech(request);

    if (!leechCheck.allowed) {
      return jsonResponse({ error: leechCheck.reason }, 403, null);
    }

    corsOrigin = resolveCorsOrigin(request);

    // ---------- 业务处理 ----------
    const imgType = url.searchParams.get('img');
    const jsonType = url.searchParams.get('json');

    await loadCount(request);

    // JSON 模式
    if (jsonType === 'h' || jsonType === 'v') {
      const darkFolder = jsonType + 'd';
      const lightFolder = jsonType + 'l';

      const selected = randomFromTwoFolders(darkFolder, lightFolder);

      if (!selected) {
        return jsonResponse({ error: '没有图片' }, 404, corsOrigin);
      }

      const imageUrl = new URL(
        `${IMAGE_BASE_PATH}/${selected.folder}/${selected.file}${IMAGE_EXT}`,
        url.origin
      ).toString();

      return jsonResponse(
        {
          theme: selected.theme,
          url: imageUrl,
        },
        200,
        corsOrigin,
        {
          'Cache-Control': 'no-store',
        }
      );
    }

    // 重定向模式
    let orientation;

    if (imgType === 'h' || imgType === 'v') {
      orientation = imgType;
    } else {
      const userAgent = request.headers.get('User-Agent') || '';
      orientation = isMobileDevice(userAgent) ? 'v' : 'h';
    }

    const darkFolder = orientation + 'd';
    const lightFolder = orientation + 'l';

    const selected = randomFromTwoFolders(darkFolder, lightFolder);

    if (!selected) {
      return textResponse('没有图片', 404, corsOrigin);
    }

    const location = `${IMAGE_BASE_PATH}/${selected.folder}/${selected.file}${IMAGE_EXT}`;

    return new Response(null, {
      status: 302,
      headers: buildHeaders(corsOrigin, {
        Location: location,
        'Cache-Control': 'no-store',
      }),
    });
  } catch (error) {
    console.error('Request failed:', error);

    return textResponse('服务器内部错误', 500, corsOrigin);
  }
}

// ========================================
// 双平台入口导出
// ========================================

// ---- 标准 Pages Functions 入口（Cloudflare + EdgeOne 均识别） ----
export async function onRequest(context) {
  return handleRequest(context.request, context.env);
}

// ---- EdgeOne Edge Functions / Workers 风格兜底入口 ----
export default {
  async fetch(request, env) {
    return handleRequest(request, env);
  },
};
