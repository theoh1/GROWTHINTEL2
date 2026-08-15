const PREVIEW_BACKEND = "https://growthintel2-pr-1.onrender.com";
const PRODUCTION_BACKEND = "https://growthintel2.onrender.com";

export default async function handler(req, res) {
  const backend =
    process.env.VERCEL_ENV === "preview"
      ? PREVIEW_BACKEND
      : PRODUCTION_BACKEND;

  const path = Array.isArray(req.query.path)
    ? req.query.path.join("/")
    : req.query.path || "";

  const searchParams = new URLSearchParams();

  for (const [key, value] of Object.entries(req.query)) {
    if (key === "path") continue;

    if (Array.isArray(value)) {
      for (const item of value) {
        searchParams.append(key, item);
      }
    } else if (value !== undefined) {
      searchParams.append(key, value);
    }
  }

  const queryString = searchParams.toString();
  const targetUrl =
    `${backend}/api/v1/${path}` +
    (queryString ? `?${queryString}` : "");

  const headers = { ...req.headers };

  delete headers.host;
  delete headers["content-length"];

  try {
    const upstream = await fetch(targetUrl, {
      method: req.method,
      headers,
      body:
        req.method === "GET" || req.method === "HEAD"
          ? undefined
          : req.body
            ? JSON.stringify(req.body)
            : undefined,
      redirect: "manual",
    });

    res.status(upstream.status);

    upstream.headers.forEach((value, key) => {
      if (key.toLowerCase() === "content-encoding") return;
      if (key.toLowerCase() === "content-length") return;

      res.setHeader(key, value);
    });

    const body = Buffer.from(await upstream.arrayBuffer());
    res.send(body);
  } catch (error) {
    console.error("Membership API proxy failed:", error);
    res.status(502).json({
      detail: "Backend temporarily unavailable",
    });
  }
}
