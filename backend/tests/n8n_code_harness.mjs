// Runs one n8n Code node ("Run Once for All Items", JavaScript) outside n8n, for tests.
// stdin: {"code": "...", "input": [{...}], "nodes": {"Node Name": [{...}]},
//         "params": {"Node Name": {...node parameters...}}}
// stdout: {"ok": true, "items": [...]} or {"ok": false, "error": "message"}
//
// Like n8n 2.41.7's data proxy: $(name) throws for a node that does not exist,
// .first()/.all() throw for a node that has not run, and .params returns the
// node's stored parameters whether or not it has run.
let raw = '';
for await (const chunk of process.stdin) raw += chunk;
const { code, input = [], nodes = {}, params = {} } = JSON.parse(raw);
const wrap = (items) => ({ first: () => items[0], all: () => items });
const $input = wrap(input.map((json) => ({ json })));
const $ = (name) => {
  if (!(name in nodes) && !(name in params)) throw new Error(`Referenced node doesn't exist: "${name}"`);
  const ran = () => {
    if (!(name in nodes)) throw new Error(`node "${name}" has not run`);
    return wrap(nodes[name].map((json) => ({ json })));
  };
  return { first: () => ran().first(), all: () => ran().all(), params: params[name] };
};
const AsyncFunction = Object.getPrototypeOf(async () => {}).constructor;
try {
  const result = await new AsyncFunction('$input', '$', code)($input, $);
  process.stdout.write(JSON.stringify({ ok: true, items: result.map((item) => item.json) }));
} catch (err) {
  process.stdout.write(JSON.stringify({ ok: false, error: String(err && err.message) }));
}
