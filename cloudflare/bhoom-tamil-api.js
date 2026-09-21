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

async function fetchBhoom(target, request, referer) {
  return fetch(target, {
    method: request?.method === "HEAD" ? "HEAD" : "GET",
    redirect: "follow",
    headers: {
      "User-Agent":
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36",
      "Accept":
        "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
      "Accept-Language": "en-US,en;q=0.9",
      "Upgrade-Insecure-Requests": "1",
      "Referer": referer || "https://bhoomtv.org/",
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


function decodeEmbeddedText(value) {
  let text = String(value || "")
    .replace(/\\\\u002f/gi, "/")
    .replace(/\\\\u0026/gi, "&")
    .replace(/\\\\\//g, "/")
    .replace(/&#x2f;/gi, "/")
    .replace(/&#47;/g, "/")
    .replace(/&amp;/g, "&");

  try {
    text = decodeURIComponent(text);
  } catch {
    // Keep the original when it is not valid URI encoding.
  }

  return text;
}

function extractStreamCandidates(html, pageUrl) {
  const normalized = decodeEmbeddedText(html);
  const found = [];
  const seen = new Set();

  const add = (value) => {
    if (!value) return;

    let url;
    try {
      url = new URL(String(value).trim(), pageUrl).toString();
    } catch {
      return;
    }

    const low = url.toLowerCase();

    if (!low.includes(".m3u8") && !low.includes(".mpd")) {
      return;
    }

    if (seen.has(url)) return;

    seen.add(url);

    found.push({
      url,
      type: low.includes(".mpd") ? "DASH" : "HLS",
    });
  };

  const absoluteRe =
    /https?:\/\/[^\s'"<>\\]+(?:\.m3u8|\.mpd)(?:\?[^\s'"<>\\]*)?/gi;

  const protocolRelativeRe =
    /\/\/[^\s'"<>\\]+(?:\.m3u8|\.mpd)(?:\?[^\s'"<>\\]*)?/gi;

  let match;

  while ((match = absoluteRe.exec(normalized))) {
    add(match[0]);
  }

  while ((match = protocolRelativeRe.exec(normalized))) {
    add(match[0]);
  }

  const keyValueRe =
    /(?:file|src|source|stream|url|hls|dash|playlist|manifest)\s*[:=]\s*["']((?:https?:)?\/\/[^"']+(?:\.m3u8|\.mpd)(?:\?[^"']*)?)["']/gi;

  while ((match = keyValueRe.exec(normalized))) {
    add(match[1]);
  }

  const dataAttrRe =
    /<(?:video|source|iframe|object|embed)\b[^>]*(?:src|data-src|data-url|data-file|data-stream|data-source|data)=[\"']([^\"']+)[\"']/gi;

  while ((match = dataAttrRe.exec(normalized))) {
    add(match[1]);
  }

  return found.slice(0, 6);
}

function extractEmbeddedTargets(html, pageUrl) {
  const found = [];
  const seen = new Set();

  const add = (value) => {
    if (!value) return;

    let target;
    try {
      target = new URL(decodeEmbeddedText(value), pageUrl).toString();
    } catch {
      return;
    }

    const parsed = new URL(target);

    if (!["http:", "https:"].includes(parsed.protocol)) {
      return;
    }

    if (seen.has(target)) return;
    seen.add(target);
    found.push(target);
  };

  let match;

  const iframeRe =
    /<(?:iframe|object|embed)\b[^>]*(?:src|data-src|data|data-url|data-file)=[\"']([^\"']+)[\"']/gi;

  while ((match = iframeRe.exec(html))) {
    add(match[1]);
    if (found.length >= 4) break;
  }

  if (found.length < 4) {
    const linkRe =
      /<(?:a)\b[^>]*href=[\"']([^\"']+)[\"'][^>]*>/gi;

    while ((match = linkRe.exec(html))) {
      const value = decodeEmbeddedText(match[1]);
      if (/^(?:https?:)?\/\/.+(?:player|embed|stream|watch)/i.test(value)) {
        add(value);
      }
      if (found.length >= 4) break;
    }
  }

  return found;
}

function extractScriptTargets(html, pageUrl) {
  const found = [];
  const seen = new Set();
  const re = /<script\b[^>]*src=[\"']([^\"']+)[\"']/gi;
  let match;

  while ((match = re.exec(html))) {
    let target;
    try {
      target = new URL(decodeEmbeddedText(match[1]), pageUrl).toString();
    } catch {
      continue;
    }

    if (!/^https?:/i.test(target) || seen.has(target)) {
      continue;
    }

    seen.add(target);
    found.push(target);

    if (found.length >= 2) {
      break;
    }
  }

  return found;
}

async function validateStream(candidate, referer) {
  try {
    const headers = {
      "User-Agent":
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/153 Safari/537.36",
      "Accept":
        candidate.type === "DASH"
          ? "application/dash+xml,application/xml,text/xml,*/*"
          : "application/vnd.apple.mpegurl,application/x-mpegURL,text/plain,*/*"
    };

    if (referer) {
      headers["Referer"] = referer;
    }

    const response = await fetch(candidate.url, {
      redirect: "follow",
      headers,
    });

    const body = await response.text();

    if (!response.ok) {
      return {
        usable: false,
        status: response.status,
        reason: "HTTP_" + response.status,
      };
    }

    if (candidate.type === "HLS") {
      if (!body.trimStart().startsWith("#EXTM3U")) {
        return {
          usable: false,
          status: response.status,
          reason: "INVALID_HLS_MANIFEST",
        };
      }
    } else if (!/<MPD\b/i.test(body)) {
      return {
        usable: false,
        status: response.status,
        reason: "INVALID_MPD",
      };
    }

    return {
      usable: true,
      status: response.status,
      reason:
        candidate.type === "HLS"
          ? "VALID_HLS"
          : "VALID_MPD",
    };
  } catch (error) {
    return {
      usable: false,
      status: null,
      reason:
        "VALIDATION_ERROR:" +
        String(error),
    };
  }
}

async function scanOneChannel(channel) {
  const scannedAt = new Date().toISOString();

  try {
    const queue = [
      {
        url: channel.url,
        depth: 0,
        kind: "channel",
      },
    ];

    const visited = new Set();
    const candidates = [];
    const candidateSeen = new Set();
    const pageReasons = [];
    let pagesFetched = 0;

    const addCandidates = (html, pageUrl, source) => {
      const rows = extractStreamCandidates(html, pageUrl);

      for (const candidate of rows) {
        if (candidateSeen.has(candidate.url)) continue;

        candidateSeen.add(candidate.url);
        candidates.push({
          ...candidate,
          captured_from: source,
        });

        console.log(
          JSON.stringify({
            channel: channel.name,
            stage: "CAPTURE",
            type: candidate.type,
            url: candidate.url,
            from: source,
          })
        );

        if (candidates.length >= 6) break;
      }
    };

    while (queue.length && pagesFetched < 6 && candidates.length < 6) {
      const current = queue.shift();

      if (!current || visited.has(current.url) || current.depth > 2) {
        continue;
      }

      visited.add(current.url);
      pagesFetched++;

      const response = await fetchBhoom(
        current.url,
        undefined,
        current.parent || channel.url
      );

      const html = await response.text();

      console.log(
        JSON.stringify({
          channel: channel.name,
          stage: "PAGE",
          depth: current.depth,
          kind: current.kind,
          url: current.url,
          status: response.status,
          bytes: html.length,
        })
      );

      if (!response.ok) {
        pageReasons.push(
          current.kind.toUpperCase() +
          "_HTTP_" +
          response.status
        );
        continue;
      }

      addCandidates(
        html,
        current.url,
        current.kind
      );

      if (candidates.length >= 6) {
        break;
      }

      const embeds = extractEmbeddedTargets(
        html,
        current.url
      );

      for (const embed of embeds) {
        if (!visited.has(embed) && queue.length < 6) {
          queue.push({
            url: embed,
            depth: current.depth + 1,
            kind: "iframe",
            parent: current.url,
          });

          console.log(
            JSON.stringify({
              channel: channel.name,
              stage: "PLAYER",
              type: "iframe",
              url: embed,
              parent: current.url,
            })
          );
        }
      }

      // Some pages put the player configuration only in an external JS file.
      // Inspect a small number of scripts only when no stream has been found yet.
      if (!candidates.length && current.depth === 0) {
        for (const script of extractScriptTargets(html, current.url)) {
          if (!visited.has(script) && queue.length < 6) {
            queue.push({
              url: script,
              depth: current.depth + 1,
              kind: "script",
              parent: current.url,
            });

            console.log(
              JSON.stringify({
                channel: channel.name,
                stage: "PLAYER",
                type: "script",
                url: script,
                parent: current.url,
              })
            );
          }
        }
      }
    }

    const validations = [];
    const streams = [];

    for (let index = 0; index < Math.min(candidates.length, 6); index++) {
      const candidate = candidates[index];

      console.log(
        JSON.stringify({
          channel: channel.name,
          stage: "VALIDATE",
          number: index + 1,
          total: Math.min(candidates.length, 6),
          type: candidate.type,
          url: candidate.url,
        })
      );

      const validation = await validateStream(
        candidate,
        channel.url
      );

      validations.push({
        ...validation,
        url: candidate.url,
        type: candidate.type,
        captured_from: candidate.captured_from,
      });

      console.log(
        JSON.stringify({
          channel: channel.name,
          stage: "VALIDATION",
          type: candidate.type,
          url: candidate.url,
          usable: validation.usable,
          status: validation.status,
          reason: validation.reason,
        })
      );

      if (validation.usable) {
        streams.push({
          url: candidate.url,
          type: candidate.type,
          headers: {
            referer: channel.url,
            "user-agent":
              "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/153 Safari/537.36",
          },
          validation,
          captured_from: candidate.captured_from,
        });

        break;
      }
    }

    let reason;

    if (streams.length) {
      reason = null;
    } else if (candidates.length) {
      reason = validations
        .map((x) => x.reason)
        .join("|");
    } else if (pageReasons.length) {
      reason = pageReasons.join("|");
    } else {
      reason = "NO_STREAM_FOUND";
    }

    return {
      ...channel,
      streams,
      last_scan_at: scannedAt,
      scan: {
        captured: candidates.length,
        usable: streams.length,
        pages_fetched: pagesFetched,
        reason,
        validations,
        scanned_at: scannedAt,
      },
    };
  } catch (error) {
    return {
      ...channel,
      streams: [],
      last_scan_at: scannedAt,
      scan: {
        captured: 0,
        usable: 0,
        reason:
          "SCAN_ERROR:" +
          String(error),
        scanned_at: scannedAt,
      },
    };
  }
}

async function getAllInventory(env) {
  const rows = [
    ...(await getInventory(env, "tamil")),
    ...(await getInventory(env, "local"))
  ];

  const map = new Map();

  for (const row of rows) {
    if (row && row.url) {
      map.set(row.url, row);
    }
  }

  return Array.from(map.values()).sort(
    (a, b) =>
      String(a.name || "").localeCompare(
        String(b.name || "")
      )
  );
}

async function saveAllInventory(env, rows) {
  await putInventory(
    env,
    "tamil",
    rows.filter(
      (x) => x.section !== "local"
    )
  );

  await putInventory(
    env,
    "local",
    rows.filter(
      (x) => x.section === "local"
    )
  );

  const all =
    await getAllInventory(env);

  await env.BHOOM_CHANNELS.put(
    INVENTORY_KEYS.all,
    JSON.stringify(all)
  );

  return all;
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


    if (url.pathname === "/scan-status") {
      const inventory =
        await getAllInventory(env);

      let scanned = 0;
      let usable = 0;
      let failed = 0;

      for (const channel of inventory) {
        if (channel.last_scan_at) {
          scanned++;
        }

        const streams =
          Array.isArray(
            channel.streams
          )
            ? channel.streams
            : [];

        if (streams.length) {
          usable++;
        } else if (
          channel.last_scan_at
        ) {
          failed++;
        }
      }

      return json({
        ok: true,
        total: inventory.length,
        scanned,
        usable,
        failed,
        remaining:
          inventory.length -
          scanned
      });
    }

    if (url.pathname === "/auto-scan") {
      const requestedBatch =
        Number(
          url.searchParams.get(
            "batch"
          ) || "4"
        );

      const batchSize =
        Math.min(
          6,
          Math.max(
            1,
            Number.isFinite(
              requestedBatch
            )
              ? requestedBatch
              : 4
          )
        );

      const inventory =
        await getAllInventory(env);

      const start =
        inventory.findIndex(
          (channel) =>
            !channel.last_scan_at
        );

      if (start < 0) {
        return new Response(
          "<html><body><h2>Scan complete</h2><p>All " +
            inventory.length +
            " channels have been scanned.</p><p><a href='/scan-status'>View status JSON</a></p></body></html>",
          {
            headers: {
              ...cors(),
              "Content-Type":
                "text/html; charset=utf-8"
            }
          }
        );
      }

      const selected =
        inventory.slice(
          start,
          start + batchSize
        );

      const scanned = [];

      for (
        const channel of selected
      ) {
        const result =
          await scanOneChannel(
            channel
          );

        scanned.push(result);

        console.log(
          JSON.stringify({
            channel:
              result.name,
            captured:
              result.scan.captured,
            usable:
              result.scan.usable,
            reason:
              result.scan.reason
          })
        );
      }

      const updated =
        inventory.map(
          (channel) => {
            const replacement =
              scanned.find(
                (x) =>
                  x.url ===
                  channel.url
              );

            return replacement ||
              channel;
          }
        );

      const all =
        await saveAllInventory(
          env,
          updated
        );

      const usable =
        scanned.filter(
          (x) =>
            Array.isArray(
              x.streams
            ) &&
            x.streams.length
        ).length;

      const next =
        all.findIndex(
          (channel) =>
            !channel.last_scan_at
        );

      let lines =
        scanned
          .map((x) => {
            const status =
              x.streams &&
              x.streams.length
                ? "USABLE"
                : "FAILED";

            const scan = x.scan || {};
            const details =
              status +
              " | " +
              x.name +
              " | CAPTURED=" +
              Number(scan.captured || 0) +
              " | PAGES=" +
              Number(scan.pages_fetched || 0) +
              " | " +
              (scan.reason || "OK");

            return details;
          })
          .join("<br>");

      let html =
        "<html><head>";

      if (next >= 0) {
        html +=
          "<meta http-equiv='refresh' content='1;url=/auto-scan?batch=" +
          batchSize +
          "'>";
      }

      html +=
        "</head><body>" +
        "<h2>Bhoom stream scan</h2>" +
        "<p>Scanned: " +
        Math.min(
          start + selected.length,
          all.length
        ) +
        " / " +
        all.length +
        "</p>" +
        "<p>Usable in this batch: " +
        usable +
        " / " +
        scanned.length +
        "</p>" +
        "<hr>" +
        lines +
        "<hr>" +
        (
          next >= 0
            ? "<p>Continuing automatically...</p>"
            : "<p><b>SCAN COMPLETE</b></p>"
        ) +
        "</body></html>";

      return new Response(
        html,
        {
          headers: {
            ...cors(),
            "Content-Type":
              "text/html; charset=utf-8"
          }
        }
      );
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
