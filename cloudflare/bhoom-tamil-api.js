const BASE = "https://bhoomtv.org";

const INVENTORY_KEYS = { tamil: "inventory:tamil", local: "inventory:local", all: "inventory:all" };\n\nconst ROUTES = {
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


function textOnly(value) {
  return String(value || "").replace(/\\s+/g, " ").trim();
}

function extractLiveChannels(html, section) {
  const rows = [];
  const seen = new Set();
  const re = /<a\\b[^>]*href=["']([^"']*\\/live\\/[^"']*)["'][^>]*>([\\s\\S]*?)<\\/a>/gi;
  let m;

  while ((m = re.exec(html))) {
    let href;
    try {
      href = new URL(m[1], BASE).toString();
    } catch {
      continue;
    }

    if (seen.has(href)) continue;

    const name = textOnly(
      m[2]
        .replace(/<script[\\s\\S]*?<\\/script>/gi, " ")
        .replace(/<style[\\s\\S]*?<\\/style>/gi, " ")
        .replace(/<[^>]+>/g, " ")
    );

    const parsed = new URL(href);
    const slug = parsed.pathname.split("/").filter(Boolean).pop() || "channel";

    seen.add(href);
    rows.push({
      name: name || slug.replace(/[-_]+/g, " ").replace(/\\b\\w/g, x => x.toUpperCase()),
      slug,
      url: href,
      section,
      discovered_at: new Date().toISOString()
    });
  }

  return rows;
}

function findNext(html, currentPage) {
  const re = /<a\\b[^>]*href=["']([^"']+)["'][^>]*>([\\s\\S]*?)<\\/a>/gi;
  const candidates = [];
  let m;

  while ((m = re.exec(html))) {
    const href = m[1];
    const label = textOnly(m[2]).toLowerCase();

    let absolute;
    try {
      absolute = new URL(href, BASE).toString();
    } catch {
      continue;
    }

    const pageMatch = absolute.match(/\\/page\\/(\\d+)\\/?$/i);
    if (pageMatch && Number(pageMatch[1]) > currentPage) {
      candidates.push({ page: Number(pageMatch[1]), url: absolute });
    }

    if (label === "next" || label === "next page" || label === "»" || label === "›") {
      return absolute;
    }
  }

  candidates.sort((a, b) => a.page - b.page);
  return candidates.length ? candidates[0].url : null;
}

async function getInventory(env, section) {
  const value = await env.BHOOM_CHANNELS.get(INVENTORY_KEYS[section]);
  if (!value) return [];

  try {
    const rows = JSON.parse(value);
    return Array.isArray(rows) ? rows : [];
  } catch {
    return [];
  }
}

async function putInventory(env, section, rows) {
  const map = new Map();
  for (const row of rows) {
    if (row && row.url) map.set(row.url, row);
  }

  const clean = Array.from(map.values()).sort((a, b) =>
    String(a.name || "").localeCompare(String(b.name || ""))
  );

  await env.BHOOM_CHANNELS.put(
    INVENTORY_KEYS[section],
    JSON.stringify(clean)
  );

  return clean;
}

async function collectPage(env, request, section, pageNumber) {
  const base = new URL(ROUTES["/" + section], BASE);
  const target = pageNumber === 1
    ? base.toString()
    : base.toString().replace(/\\/$/, "") + "/page/" + pageNumber + "/";

  const upstream = await fetch(target, {
    redirect: "follow",
    headers: {
      "User-Agent": request.headers.get("User-Agent") || "Mozilla/5.0",
      "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
      "Accept-Language": "en-US,en;q=0.9"
    }
  });

  const html = await upstream.text();

  if (!upstream.ok) {
    return {
      ok: false,
      section,
      page: pageNumber,
      status: upstream.status,
      channels: 0,
      complete: true
    };
  }

  const rows = extractLiveChannels(html, section);
  const existing = await getInventory(env, section);
  const combined = await putInventory(env, section, existing.concat(rows));
  const next = findNext(html, pageNumber);

  return {
    ok: true,
    section,
    page: pageNumber,
    status: upstream.status,
    channels_on_page: rows.length,
    inventory_count: combined.length,
    next_page: next,
    complete: !next
  };
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
        kv_binding: "BHOOM_CHANNELS",\n        endpoints: ["/tamil", "/local", "/proxy?url=...", "/collect?section=tamil&page=1", "/inventory"]
      });
    }


    if (url.pathname === "/inventory") {
      const section = url.searchParams.has("tamil")
        ? "tamil"
        : url.searchParams.has("local")
          ? "local"
          : "all";

      let channels = [];

      if (section === "all") {
        channels = [
          ...(await getInventory(env, "tamil")),
          ...(await getInventory(env, "local"))
        ];
        const map = new Map(channels.map(x => [x.url, x]));
        channels = Array.from(map.values());
      } else {
        channels = await getInventory(env, section);
      }

      return json({
        ok: true,
        section,
        count: channels.length,
        channels
      });
    }

    if (url.pathname === "/collect") {
      const section = String(url.searchParams.get("section") || "").toLowerCase();
      const page = Math.max(1, Number(url.searchParams.get("page") || "1"));

      if (!["tamil", "local"].includes(section)) {
        return json({
          ok: false,
          error: "section must be tamil or local"
        }, 400);
      }

      try {
        return json(await collectPage(env, request, section, page));
      } catch (error) {
        return json({
          ok: false,
          error: String(error),
          section,
          page
        }, 502);
      }
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
