const PREVIEW_BACKEND = "https://growthintel2-pr-1.onrender.com";
const PRODUCTION_BACKEND = "https://growthintel2.onrender.com";

export default async function handler(req, res) {
  const backend =
    process.env.VERCEL_ENV === "preview"
      ? PREVIEW_BACKEND
      : PRODUCTION_BACKEND;

  const targetUrl = `${backend}${req.url}`;

  const headers = { ...req.headers };

  delete headers.host;
  delete headers["content-length"];
  delete headers.connection;

  try {
    let body;

    if (req.method !== "GET" && req.method !== "HEAD") {
      if (req.body !== undefined && req.body !== null) {
        body =
          typeof req.body === "string" || Buffer.isBuffer(req.body)
            ? req.body
            : JSON.stringify(req.body);
      }
    }

    const upstream = await fetch(targetUrl, {
      method: req.method,
      headers,
      body,
      redirect: "manual",
    });

    res.status(upstream.status);

    upstream.headers.forEach((value, key) => {
      const lowerKey = key.toLowerCase();

      if (
        lowerKey === "content-encoding" ||
        lowerKey === "content-length" ||
        lowerKey === "transfer-encoding"
      ) {
        return;
      }

      res.setHeader(key, value);
    });

    if (typeof upstream.headers.getSetCookie === "function") {
      const cookies = upstream.headers.getSetCookie();

      if (cookies.length) {
        res.setHeader("set-cookie", cookies);
      }
    }

    const responseBody = Buffer.from(await upstream.arrayBuffer());

    res.send(responseBody);
  } catch (error) {
    console.error("Growth Intel API proxy failed:", error);

    res.status(502).json({
      detail: "Backend temporarily unavailable",
    });
  }
}
