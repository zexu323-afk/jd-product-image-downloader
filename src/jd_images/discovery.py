"""Product-owned media discovery extracted from the original collector.

Page tokens stay inside the user-owned browser page; URLs remain in memory.
"""

from __future__ import annotations
import html
import re
import urllib.parse
from typing import Any

PLATFORM_RESTRICTION_PATTERNS: tuple[tuple[str, str], ...] = (
    ("VERIFY_SHOPPING", "验证一下，购物无忧"),
    ("QUICK_VERIFY", "快速验证"),
    ("CAPTCHA", "验证码"),
    ("SLIDER_VERIFY", "滑动验证"),
    ("SECURITY_VERIFY", "安全验证"),
    ("ACCESS_VERIFY", "访问验证"),
    ("COMPLETE_VERIFY", "请完成验证"),
    ("ACCESS_TOO_FREQUENT", "访问过于频繁"),
    ("ABNORMAL_REQUEST", "访问请求存在异常"),
    ("LOGIN_REQUIRED", "登录后查看"),
    ("FORBIDDEN", "403 Forbidden"),
)

def _platform_restriction_code(body_text: str, page_url: str = "") -> str | None:
    lowered_url = (page_url or "").lower()
    if "passport.jd.com" in lowered_url or "/login" in lowered_url:
        return "LOGIN_REQUIRED"
    normalized = re.sub(r"\s+", "", body_text or "")
    for code, text in PLATFORM_RESTRICTION_PATTERNS:
        if re.sub(r"\s+", "", text) in normalized:
            return code
    if "forbidden" in normalized.lower():
        return "FORBIDDEN"
    if "暂时无法展示该商品的信息" in normalized:
        return "PRODUCT_INFO_RESTRICTED"
    return None

def _detail_image_urls(page, sku: str) -> list[str]:
    """Read JD's product-owned description payload without persisting page tokens."""
    try:
        page.wait_for_function(
            "() => globalThis.PSign && typeof globalThis.PSign.sign === 'function'",
            timeout=15_000,
        )
        content = page.evaluate(
            r"""async (sku) => {
              const readCookie = (name) => {
                const row = document.cookie.split('; ').find(item => item.startsWith(name + '='));
                return row ? decodeURIComponent(row.slice(name.length + 1)) : '';
              };
              const body = JSON.stringify({mainSkuId: sku, skuId: sku});
              const digestBytes = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(body));
              const bodyDigest = [...new Uint8Array(digestBytes)]
                .map(value => value.toString(16).padStart(2, '0')).join('');
              const timestamp = Date.now();
              const base = {
                appid: 'item-v3', functionId: 'pc_description_channel',
                client: 'pc', clientVersion: '1.0.0', t: timestamp, body
              };
              const signed = await globalThis.PSign.sign({...base, body: bodyDigest});
              const pageToken = await new Promise(resolve => {
                let done = false;
                const finish = value => {
                  if (done) return;
                  done = true;
                  resolve((value && value.jsToken) || value || '');
                };
                const timer = setTimeout(() => finish(''), 8000);
                try {
                  globalThis.getJsToken(value => { clearTimeout(timer); finish(value); });
                } catch (_) {
                  clearTimeout(timer); finish('');
                }
              });
              const callback = '__jd_monitor_detail_' + timestamp;
              const query = new URLSearchParams({
                ...base,
                h5st: (signed && signed.h5st) || '',
                'x-api-eid-token': pageToken || readCookie('3AB9D23F7A4B3CSS'),
                uuid: readCookie('__jda'), loginType: '3', mainSkuId: sku, skuId: sku,
                charset: 'utf-8', cdn: '2', callback
              });
              return await new Promise((resolve, reject) => {
                const tag = document.createElement('script');
                let timer;
                const clear = () => {
                  clearTimeout(timer); tag.remove();
                  try { delete globalThis[callback]; } catch (_) {}
                };
                globalThis[callback] = payload => {
                  clear();
                  if (!payload || typeof payload.content !== 'string') {
                    reject(new Error('DETAIL_CONTENT_MISSING'));
                  } else {
                    resolve(payload.content);
                  }
                };
                tag.onerror = () => { clear(); reject(new Error('DETAIL_REQUEST_FAILED')); };
                timer = setTimeout(() => { clear(); reject(new Error('DETAIL_REQUEST_TIMEOUT')); }, 20000);
                tag.src = 'https://api.m.jd.com/description/channel?' + query;
                document.head.appendChild(tag);
              });
            }""",
            sku,
        )
    except Exception as exc:
        raise RuntimeError("DETAIL_SOURCE_UNAVAILABLE") from exc

    decoded = html.unescape(str(content or "").replace("\\u002F", "/").replace("\\/", "/"))
    candidates = re.findall(r'(?:https?:)?//[^\s"\'<>\\)]+', decoded, re.I)
    result: list[str] = []
    seen: set[str] = set()
    for value in candidates:
        url = "https:" + value if value.startswith("//") else value
        parsed = urllib.parse.urlparse(url)
        if (
            parsed.scheme.lower() != "https"
            or not parsed.hostname
            or not parsed.hostname.lower().endswith(".360buyimg.com")
            or "/jfs/" not in parsed.path.lower()
        ):
            continue
        key = url.split("?", 1)[0]
        if key not in seen:
            seen.add(key)
            result.append(url)
    return result

def _material_tasks(page, sku: str | None = None) -> list[dict]:
    """Discover media from product-owned containers, never from all page images."""
    discovered = page.evaluate(r"""() => {
      const urlOf = (node) => {
        if (!node) return '';
        for (const key of ['src', 'data-lazyload', 'data-src', 'data-url', 'data-origin', 'original']) {
          const value = node.getAttribute && node.getAttribute(key);
          if (value && !value.startsWith('data:')) return value;
        }
        return node.currentSrc || '';
      };
      const urls = (selector) => [...document.querySelectorAll(selector)].map(urlOf).filter(Boolean);
      let main = urls('.image-carousel-track .item img.image');
      if (!main.length) main = urls('#spec-list img, .spec-items img');
      if (!main.length) {
        const product = globalThis.pageConfig && globalThis.pageConfig.product;
        main = product && Array.isArray(product.imageList) ? product.imageList : [];
      }
      const video = [...document.querySelectorAll('video, video source')]
        .filter(node => node.closest(
          '.product-intro, #spec-n1, #preview, .image-carousel-track, [class*="preview"]'
        ))
        .flatMap(node => [node.currentSrc, node.src, node.getAttribute && node.getAttribute('src')])
        .filter(value => value && /\.mp4(?:[?#]|$)/i.test(value));
      return {main, detail: [], video};
    }""")
    if sku is not None:
        discovered["detail"] = _detail_image_urls(page, sku)

    def normalize(value: Any, category: str) -> str:
        url = str(value or "").strip().replace("\\/", "/")
        if url.startswith("//"):
            url = "https:" + url
        elif url.startswith("http://"):
            url = "https://" + url[7:]
        url = url.replace(".avif", "")
        if category == "MAIN_IMAGE" and "jfs/" in url:
            url = "https://img10.360buyimg.com/pcpubliccms/s800x800_" + url[url.index("jfs/"):]
        return url

    tasks: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for category, field, stem in (
        ("MAIN_IMAGE", "main", "main-image"),
        ("MAIN_VIDEO", "video", "main-video"),
        ("DETAIL_IMAGE", "detail", "detail-image"),
    ):
        for index, value in enumerate(discovered.get(field, []), 1):
            url = normalize(value, category)
            if not re.match(r"^https://", url, re.I):
                continue
            key = (category, url.split("?", 1)[0])
            if category != "MAIN_IMAGE":
                if key in seen:
                    continue
                seen.add(key)
            tasks.append({"url": url, "category": category, "stem": f"{stem}-{index:02d}"})
    return tasks
