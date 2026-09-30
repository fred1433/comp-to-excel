// Serves the static page at https://theaipipe.com/comp-to-excel/ ; nothing here calls an API or a database.
export default {
  async fetch(request, env) {
    const res = await env.ASSETS.fetch(request);
    const out = new Response(res.body, res);
    out.headers.set("X-Robots-Tag", "noindex, nofollow");
    return out;
  },
};
