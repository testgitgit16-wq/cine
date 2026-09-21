const BASE = "https://bhoomtv.org";

const ROUTES = {
  "/tamil": "/channel/tamil/",
  "/local": "/channel/tamil-local-tv/",
};

function cors() {
  return {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET,HEAD,OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "Cache-Control": "no-store"
  };
}

function json(data, status = 200) {
  return new Response(JSON.stringify(data, null, 2), {
    status,
    headers: {
      ...cors(),
      "Content-Type": "application/json; charset=utf-8"
    }
  });
}

function allowedPath(pathname) {
  return (
    pathname === "/" ||
    pathname.startsWith("/channel/") ||
    pathname.startsWith("/live/") ||
    pathname === "/wp-sitemap.xml" ||
    pathname === "/sitemap.xml" ||
    pathname === "/sitemap_index.xml" ||
    pathname.startsWith("/wp-sitemap-")
  );
}

function targetFromRequest(request) {
  const url = new URL(request.url);

  if (ROUTES[url.pathname]) {
    return new URL(ROUTES[url.pathname], BASE).toString();
  }

  if (url.pathname !== "/proxy") {
    return null;
  }

  const target = url.searchParams.get("url");
  if (!target) return null;

  let parsed;
  try {
    parsed = new URL(target);
  } catch {
    return null;
  }

  if (!["http:", "https:"].includes(parsed.protocol)) return null;
  if (parsed.hostname !== "bhoomtv.org" && parsed.hostname !== "www.bhoomtv.org") return null;
  if (!allowedPath(parsed.pathname)) return null;

  return parsed.toString();
}

export default {
  async fetch(request) {
    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: cors() });
    }

    if (!["GET", "HEAD"].includes(request.method)) {
      return json({ ok: false, error: "Method not allowed" }, 405);
    }

    const url = new URL(request.url);

    if (url.pathname === "/") {
      return json({
        ok: true,
        worker: "bhoom-tamil-api",
        mode: "authorized-source-proxy",
        endpoints: ["/tamil", "/local", "/proxy?url=..."]
      });
    }

    const target = targetFromRequest(request);

    if (!target) {
      return json({
        ok: false,
        error: "Unsupported or unauthorized target"
      }, 400);
    }

    try {
      const upstream = await fetch(target, {
        method: request.method,
        redirect: "follow",
        headers: {
          "User-Agent": request.headers.get("User-Agent") ||
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/153 Safari/537.36",
          "Accept": request.headers.get("Accept") ||
            "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
          "Accept-Language": request.headers.get("Accept-Language") || "en-US,en;q=0.9"
        }
      });

      const headers = new Headers(cors());
      for (const name of ["content-type", "cache-control", "etag", "last-modified", "location"]) {
        const value = upstream.headers.get(name);
        if (value) headers.set(name, value);
      }

      return new Response(request.method === "HEAD" ? null : upstream.body, {
        status: upstream.status,
        headers
      });
    } catch (error) {
      return json({
        ok: false,
        error: String(error)
      }, 502);
    }
  }
};
