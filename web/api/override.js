// Shared status-override store for the Sprinklr Support Tickets tab.
// GET  -> current overrides.json ({case_id: status})
// POST {case_id, status} -> upsert (status "Auto" removes the override)
// Stored as overrides.json at the repo root via the GitHub Contents API, so a
// correction made in the dashboard holds for every viewer and the daily pipeline
// bakes it into tickets.json + status_truth. Needs env GH_OVERRIDE_TOKEN
// (fine-grained token, Contents: read+write on the repo).
const OWNER = "HarshKPB";
const REPO = "netflix-ms-sla-refresh";
const PATH = "overrides.json";
const BRANCH = "main";
const ALLOWED = new Set(["Auto", "Awaiting us", "Awaiting Sprinklr", "Resolved"]);
const API = `https://api.github.com/repos/${OWNER}/${REPO}/contents/${PATH}`;

async function ghGet(token) {
  const r = await fetch(`${API}?ref=${BRANCH}`, {
    headers: { Authorization: `Bearer ${token}`, Accept: "application/vnd.github+json", "User-Agent": "netflix-sla" },
  });
  if (r.status === 404) return { json: {}, sha: null };
  if (!r.ok) throw new Error(`github get ${r.status}`);
  const d = await r.json();
  let json = {};
  try { json = JSON.parse(Buffer.from(d.content || "", "base64").toString("utf8") || "{}") || {}; } catch (e) { json = {}; }
  return { json, sha: d.sha };
}

module.exports = async (req, res) => {
  const token = process.env.GH_OVERRIDE_TOKEN;
  try {
    if (req.method === "GET") {
      if (!token) { res.status(200).json({}); return; }
      const { json } = await ghGet(token);
      res.status(200).json(json);
      return;
    }
    if (req.method === "POST") {
      if (!token) { res.status(500).json({ error: "GH_OVERRIDE_TOKEN not set" }); return; }
      let body = req.body;
      if (typeof body === "string") { try { body = JSON.parse(body); } catch (e) { body = {}; } }
      const cid = String((body && body.case_id) || "").slice(0, 40);
      const status = String((body && body.status) || "");
      if (!cid || !ALLOWED.has(status)) { res.status(400).json({ error: "bad case_id or status" }); return; }
      const { json, sha } = await ghGet(token);
      if (status === "Auto") delete json[cid]; else json[cid] = status;
      const put = await fetch(API, {
        method: "PUT",
        headers: { Authorization: `Bearer ${token}`, Accept: "application/vnd.github+json", "User-Agent": "netflix-sla", "Content-Type": "application/json" },
        body: JSON.stringify({
          message: `override: ${cid} -> ${status}`,
          content: Buffer.from(JSON.stringify(json, null, 2) + "\n").toString("base64"),
          sha: sha || undefined,
          branch: BRANCH,
        }),
      });
      if (!put.ok) { res.status(500).json({ error: `github put ${put.status}`, detail: (await put.text()).slice(0, 200) }); return; }
      res.status(200).json({ ok: true, overrides: json });
      return;
    }
    res.status(405).json({ error: "method not allowed" });
  } catch (e) {
    res.status(500).json({ error: String(e).slice(0, 200) });
  }
};
