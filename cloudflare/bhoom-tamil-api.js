const BASE = "https://bhoomtv.org";

const ROUTES = {
  "/tamil": "/channel/tamil/",
  "/local": "/channel/tamil-local-tv/",
};

const INVENTORY_KEYS = {
  tamil: "inventory:tamil",
  local: "inventory:local",
  all: "inventory:all",
};

function cors() {
  return {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET,HEAD,OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "Cache-Control": "no-store",
  };
}

function json(data, status = 200) {
  return new Response(JSON.stringify(data, null, 2), {
    status,
    headers: {
      ...cors(),
      "Content-Type": "application/json; charset=utf-8",
    },
  });
}

function normalizeText(value) {
  return String(value || "").replace(/\s+/g, " ").trim();
}

function absoluteUrl(value) {
  try {
    return new URL(value, BASE).toString();
  } catch {
    return null;
  }
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
  if (!target) {
    return null;
  }

  let parsed;

  try {
    parsed = new URL(target);
  } catch {
    return null;
  }

  if (
    parsed.hostname !== "bhoomtv.org" &&
    parsed.hostname !== "www.bhoomtv.org"
  ) {
    return null;
  }

  if (!allowedPath(parsed.pathname)) {
    return null;
  }

  return parsed.toString();
}

function extractLiveChannels(html, section) {
  const rows = [];
  const seen = new Set();

  const re =
    /<a\b[^>]*href=["']([^"']*\/live\/[^"']*)["'][^>]*>([\s\S]*?)<\/a>/gi;

  let match;

  while ((match = re.exec(html))) {
    const href = absoluteUrl(match[1]);
    if (!href || seen.has(href)) {
      continue;
    }

    const parsed = new URL(href);

    const innerText = match[2]
      .replace(/<script[\s\S]*?<\/script>/gi, " ")
      .replace(/<style[\s\S]*?<\/style>/gi, " ")
      .replace(/<[^>]+>/g, " ");

    const slug =
      parsed.pathname.split("/").filter(Boolean).pop() || "channel";

    let name = normalizeText(innerText);

    if (!name) {
      name = slug
        .replace(/[-_]+/g, " ")
        .replace(/\b\w/g, (x) => x.toUpperCase());
    }

    seen.add(href);

    rows.push({
      name,
      slug,
      url: href,
      section,
      discovered_at: new Date().toISOString(),
    });
  }

  return rows;
}

function findExplicitNextPage(html, currentPage) {
  const candidates = [];

  const re = /<a\b[^>]*href=["']([^"']+)["'][^>]*>([\s\S]*?)<\/a>/gi;

  let match;

  while ((match = re.exec(html))) {
    const href = absoluteUrl(match[1]);
    if (!href) {
      continue;
    }

    const label = normalizeText(
      match[2]
        .replace(/<script[\s\S]*?<\/script>/gi, " ")
        .replace(/<style[\s\S]*?<\/style>/gi, " ")
        .replace(/<[^>]+>/g, " ")
    ).toLowerCase();

    if (
      label === "next" ||
      label === "next page" ||
      label === "»" ||
      label === "›"
    ) {
      return href;
    }

    const pageMatch = href.match(/\/page\/(\d+)\/?$/i);

    if (pageMatch && Number(pageMatch[1]) > currentPage) {
      candidates.push({
        page: Number(pageMatch[1]),
        url: href,
      });
    }
  }

  candidates.sort((a, b) => a.page - b.page);

  return candidates.length ? candidates[0].url : null;
}

async function getInventory(env, section) {
  const value = await env.BHOOM_CHANNELS.get(INVENTORY_KEYS[section]);

  if (!value) {
    return [];
  }

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
    if (row && row.url) {
      map.set(row.url, row);
    }
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

async function fetchBhoom(target, request) {
  return fetch(target, {
    method: request.method === "HEAD" ? "HEAD" : "GET",
    redirect: "follow",
    headers: {
      "User-Agent":
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36",
      "Accept":
        "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
      "Accept-Language": "en-US,en;q=0.9",
      "Upgrade-Insecure-Requests": "1",
      "Referer": "https://bhoomtv.org/",
    },
  });
}

async function collectPage(env, request, section, pageNumber) {
  const base = new URL(ROUTES["/" + section], BASE);

  const target =
    pageNumber === 1
      ? base.toString()
      : base.toString().replace(/\/$/, "") +
        "/page/" +
        pageNumber +
        "/";

  const upstream = await fetchBhoom(target, request);
  const html = await upstream.text();

  if (!upstream.ok) {
    return {
      ok: false,
      section,
      page: pageNumber,
      status: upstream.status,
      channels_on_page: 0,
      inventory_count: (await getInventory(env, section)).length,
      complete: true,
      next_page: null,
      stopped_reason: "HTTP_" + upstream.status,
    };
  }

  const rows = extractLiveChannels(html, section);
  const existing = await getInventory(env, section);
  const combined = await putInventory(
    env,
    section,
    existing.concat(rows)
  );

  const explicitNext = findExplicitNextPage(html, pageNumber);

  let nextPage = explicitNext;

  // When the page contains channels but no explicit "next" link,
  // continue sequentially until a page returns zero channels.
  if (!nextPage && rows.length > 0) {
    nextPage =
      base.toString().replace(/\/$/, "") +
      "/page/" +
      (pageNumber + 1) +
      "/";
  }

  return {
    ok: true,
    section,
    page: pageNumber,
    status: upstream.status,
    channels_on_page: rows.length,
    inventory_count: combined.length,
    complete: rows.length === 0 || !nextPage,
    next_page: nextPage,
    stopped_reason: null,
  };
}


async function collectBatch(env, request, section, startPage, maxPages) {
  const results = [];
  let totalPages = 0;

  for (let page = startPage; page < startPage + maxPages; page++) {
    const result = await collectPage(env, request, section, page);
    results.push(result);
    totalPages++;

    if (!result.ok || result.channels_on_page === 0 || !result.next_page) {
      break;
    }
  }

  return {
    ok: true,
    section,
    start_page: startPage,
    pages_attempted: totalPages,
    results,
    inventory_count: (await getInventory(env, section)).length,
    generated_at: new Date().toISOString(),
  };
}

export default {
  async fetch(request, env) {
    if (request.method === "OPTIONS") {
      return new Response(null, {
        status: 204,
        headers: cors(),
      });
    }

    if (!["GET", "HEAD"].includes(request.method)) {
      return json(
        {
          ok: false,
          error: "Method not allowed",
        },
        405
      );
    }

    const url = new URL(request.url);

    if (url.pathname === "/") {
      return json({
        ok: true,
        worker: "bhoom-tamil-api",
        mode: "authorized-source-proxy-kv",
        kv_binding: "BHOOM_CHANNELS",
        endpoints: [
          "/tamil",
          "/local",
          "/proxy?url=...",
          "/collect?section=tamil&page=1",
          "/collect?section=local&page=1",
          "/collect-batch?section=tamil&pages=10",
          "/collect-all?pages=10",
          "/inventory",
          "/inventory?tamil",
          "/inventory?local",
        ],
      });
    }


    if (url.pathname === "/collect-batch") {
      const section = String(
        url.searchParams.get("section") || ""
      ).toLowerCase();

      const startPage = Math.max(
        1,
        Number(
          url.searchParams.get("start") || "1"
        )
      );

      const maxPages = Math.min(
        20,
        Math.max(
          1,
          Number(
            url.searchParams.get("pages") || "10"
          )
        )
      );

      if (!["tamil", "local"].includes(section)) {
        return json(
          {
            ok: false,
            error: "section must be tamil or local",
          },
          400
        );
      }

      try {
        return json(
          await collectBatch(
            env,
            request,
            section,
            startPage,
            maxPages
          )
        );
      } catch (error) {
        return json(
          {
            ok: false,
            error: String(error),
            section,
            start: startPage,
          },
          502
        );
      }
    }

    if (url.pathname === "/collect-all") {
      const maxPages = Math.min(
        20,
        Math.max(
          1,
          Number(
            url.searchParams.get("pages") || "10"
          )
        )
      );

      try {
        const tamil = await collectBatch(
          env,
          request,
          "tamil",
          1,
          maxPages
        );

        const local = await collectBatch(
          env,
          request,
          "local",
          1,
          maxPages
        );

        const all = Array.from(
          new Map(
            [
              ...(await getInventory(env, "tamil")),
              ...(await getInventory(env, "local")),
            ].map((item) => [item.url, item])
          ).values()
        );

        await env.BHOOM_CHANNELS.put(
          INVENTORY_KEYS.all,
          JSON.stringify(all)
        );

        return json({
          ok: true,
          max_pages_per_section: maxPages,
          tamil,
          local,
          total_channels: all.length,
          generated_at: new Date().toISOString(),
        });
      } catch (error) {
        return json(
          {
            ok: false,
            error: String(error),
          },
          502
        );
      }
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
          ...(await getInventory(env, "local")),
        ];

        const map = new Map();

        for (const channel of channels) {
          if (channel && channel.url) {
            map.set(channel.url, channel);
          }
        }

        channels = Array.from(map.values()).sort((a, b) =>
          String(a.name || "").localeCompare(String(b.name || ""))
        );
      } else {
        channels = await getInventory(env, section);
      }

      return json({
        ok: true,
        section,
        count: channels.length,
        channels,
      });
    }

    if (url.pathname === "/collect") {
      const section = String(
        url.searchParams.get("section") || ""
      ).toLowerCase();

      const page = Math.max(
        1,
        Number(url.searchParams.get("page") || "1")
      );

      if (!["tamil", "local"].includes(section)) {
        return json(
          {
            ok: false,
            error: "section must be tamil or local",
          },
          400
        );
      }

      try {
        return json(
          await collectPage(
            env,
            request,
            section,
            page
          )
        );
      } catch (error) {
        return json(
          {
            ok: false,
            error: String(error),
            section,
            page,
          },
          502
        );
      }
    }

    const target = targetFromRequest(request);

    if (!target) {
      return json(
        {
          ok: false,
          error: "Unsupported or unauthorized target",
        },
        400
      );
    }

    try {
      const upstream = await fetchBhoom(target, request);

      const headers = new Headers(cors());

      for (const name of [
        "content-type",
        "cache-control",
        "etag",
        "last-modified",
        "location",
      ]) {
        const value = upstream.headers.get(name);

        if (value) {
          headers.set(name, value);
        }
      }

      return new Response(
        request.method === "HEAD"
          ? null
          : upstream.body,
        {
          status: upstream.status,
          headers,
        }
      );
    } catch (error) {
      return json(
        {
          ok: false,
          error: String(error),
        },
        502
      );
    }
  },
};
