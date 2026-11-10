// DLR Claude guard rules (#101, hardened in #215). Mirrored in src/llm_router/claude_guard.py; both are tested
// against one case table (tests/test_claude_guard_bypasses.py). Every check fails closed: when unsure, block.
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const FILE_KEYS = ["file_path", "path", "notebook_path"];
const SEARCH_TOOLS = new Set(["grep", "glob", "ls"]);
const SHELLS = new Set(["sh", "bash", "zsh", "dash", "ksh"]);
const WRAPPERS = new Set(["sudo", "command", "builtin", "exec", "nohup", "time", "nice", "env", "xargs", "doas"]);
const GLOB_CHARS = /[*?[{]/;
const GLOB_LIMIT = 20000;
const SEARCH_LIMIT = 20000;
const DOT_SECRETS = [".env", ".env.local", ".codex"];
const FORBIDDEN_GIT = /\bgit\s+(?:\S+\s+)*push\b|\bgit\s+(?:\S+\s+)*reset\s+--hard\b|\bgit\s+(?:\S+\s+)*merge\b[^\n;|&]*\b(?:main|master)\b|\bgh\s+pr\s+merge\b|\bgh\s+pr\s+review\b[^\n;|&]*--approve\b/i;
const FEEDERS = new Set(["xargs", "parallel", "rush"]);
// Recursive-capable readers and every option the guard understands, fully spelled. Anything else (an
// abbreviation, an unknown long option, -d/--directories, a path-list option such as --files-from, -x or
// -files0-from) means the guard cannot tell what is read: the call is blocked. short: flags; shortValue: flags
// that take a value (attached or the next word); long/longValue likewise; optionalEq: long flags that may carry
// "=value"; pattern: the first plain word is the pattern unless one of explicit was given.
const READERS = {
  grep: { short: "rRnilvwxcHhsqoLzZaIFEGPUbTuyV", shortValue: "ABCmef", explicit: "ef", recursiveShort: "rR",
    long: ["--recursive", "--dereference-recursive", "--line-number", "--ignore-case", "--no-ignore-case",
      "--invert-match", "--word-regexp", "--line-regexp", "--count", "--with-filename", "--no-filename",
      "--no-messages", "--quiet", "--silent", "--only-matching", "--files-with-matches", "--files-without-match",
      "--null", "--null-data", "--text", "--fixed-strings", "--extended-regexp", "--basic-regexp", "--perl-regexp",
      "--byte-offset", "--initial-tab", "--line-buffered"],
    longValue: ["--regexp", "--file", "--include", "--exclude", "--exclude-dir", "--max-count", "--after-context",
      "--before-context", "--context"],
    explicitLong: ["--regexp", "--file"], recursiveLong: ["--recursive", "--dereference-recursive"],
    optionalEq: ["--color", "--colour"], pattern: true, always: false },
  rg: { short: "isSnNlcvwxFuLHzaoqUP0", shortValue: "egtTABCmjMfr", explicit: "ef", recursiveShort: "",
    long: ["--ignore-case", "--smart-case", "--case-sensitive", "--line-number", "--no-line-number",
      "--files-with-matches", "--files-without-match", "--count", "--count-matches", "--invert-match",
      "--word-regexp", "--line-regexp", "--fixed-strings", "--hidden", "--no-ignore", "--follow", "--json", "--files",
      "--vimgrep", "--heading", "--no-heading", "--with-filename", "--no-filename", "--only-matching", "--multiline",
      "--pcre2", "--null", "--text", "--unrestricted"],
    longValue: ["--regexp", "--file", "--glob", "--iglob", "--type", "--type-not", "--max-count", "--context",
      "--after-context", "--before-context", "--max-depth", "--threads", "--replace", "--max-filesize", "--sort",
      "--sortr"],
    explicitLong: ["--regexp", "--file"], recursiveLong: [], optionalEq: ["--color", "--colour"], pattern: true,
    always: true },
  ag: { short: "ilLcvwQsSuUfHtan", shortValue: "GgmACB", explicit: "", recursiveShort: "",
    long: ["--hidden", "--ignore-case", "--literal", "--files-with-matches", "--count", "--invert-match",
      "--word-regexp", "--follow", "--unrestricted", "--nocolor", "--nogroup", "--filename", "--nofilename",
      "--vimgrep", "--skip-vcs-ignores", "--all-text", "--all-types"],
    longValue: ["--ignore", "--file-search-regex", "--max-count", "--context", "--after", "--before", "--depth"],
    explicitLong: [], recursiveLong: [], optionalEq: ["--color"], pattern: true, always: true },
  ack: { short: "ilLcvwQhHnks", shortValue: "ABCm", explicit: "", recursiveShort: "",
    long: ["--ignore-case", "--literal", "--count", "--invert-match", "--word-regexp", "--with-filename",
      "--no-filename", "--follow", "--nofollow", "--nocolor", "--heading", "--noheading", "--smart-case", "--break",
      "--nobreak", "--group", "--nogroup"],
    longValue: ["--type", "--ignore-dir", "--max-count", "--context", "--after-context", "--before-context", "--match"],
    explicitLong: ["--match"], recursiveLong: [], optionalEq: ["--color"], pattern: true, always: true },
  tree: { short: "adflisughDFCnNpqQrtUvJxX", shortValue: "LPI", explicit: "", recursiveShort: "",
    long: ["--noreport", "--dirsfirst", "--prune", "--matchdirs", "--ignore-case", "--du", "--si"],
    longValue: ["--charset", "--filelimit", "--timefmt", "--sort"], explicitLong: [], recursiveLong: [],
    optionalEq: [], pattern: false, always: true },
};
READERS.egrep = READERS.grep;
READERS.fgrep = READERS.grep;
const FIND_LEADING = new Set(["-H", "-L", "-P", "-E", "-X", "-s", "-x", "-d"]);
const FIND_PRIMARIES = new Set(["-name", "-iname", "-path", "-ipath", "-wholename", "-iwholename", "-regex", "-iregex",
  "-regextype", "-type", "-xtype", "-maxdepth", "-mindepth", "-mtime", "-mmin", "-atime", "-amin", "-ctime", "-cmin",
  "-newer", "-anewer", "-cnewer", "-size", "-empty", "-print", "-print0", "-printf", "-prune", "-not", "-and", "-or",
  "-o", "-a", "-follow", "-xdev", "-mount", "-depth", "-perm", "-user", "-group", "-nouser", "-nogroup", "-uid",
  "-gid", "-links", "-inum", "-samefile", "-ls", "-delete", "-quit", "-readable", "-writable", "-executable", "-true",
  "-false", "-lname", "-ilname", "-noleaf", "-daystart"]);
const FIND_EXEC = new Set(["-exec", "-execdir", "-ok", "-okdir"]);
function home() {
  return process.env.HOME || os.homedir();
}

// Plain concatenation: path.join would collapse "~/link/.." before the link is followed.
function expandHome(text) {
  return text === "~" ? home() : text.startsWith("~/") ? `${home().replace(/\/+$/, "")}/${text.slice(2)}` : text;
}

function lstatOrNull(target) {
  try {
    return fs.lstatSync(target);
  } catch {
    return null;
  }
}

// The job root is DLR_WORKTREE, set by DLR's dispatch (task_dispatch.build_argv). There is no fallback: state an
// agent could write (a temp file, a later event.cwd) never decides the boundary. null when unset; the hook blocks.
export function jobRoot(env = process.env) {
  return env.DLR_WORKTREE || null;
}

// Component-wise realpath, the same walk as claude_guard.resolve_real: an existing symlink is resolved before the
// next component (so "link/../x" leaves from the link's target, as the kernel does); missing components are kept.
// A dangling or looping symlink makes the result unknowable: null, and callers block.
export function resolveReal(text, cwd) {
  const expanded = expandHome(String(text));
  let current;
  if (path.isAbsolute(expanded)) current = "/";
  else {
    current = path.resolve(String(cwd));
    try {
      current = fs.realpathSync(current);
    } catch {}
  }
  for (const segment of expanded.split("/")) {
    if (!segment || segment === ".") continue;
    if (segment === "..") {
      current = path.dirname(current);
      continue;
    }
    const next = path.join(current, segment);
    if (lstatOrNull(next)?.isSymbolicLink()) {
      try {
        current = fs.realpathSync(next);
      } catch {
        return null;
      }
    } else current = next;
  }
  return current;
}

function deniedName(text) {
  const norm = text.replace(/\\/g, "/");
  const base = path.posix.basename(norm).toLowerCase();
  if (base === ".env" || (base.startsWith(".env.") && base !== ".env.example")) return `blocked secret file access: ${base}`;
  if (base.endsWith(".p8") || base === "auth.json") return `blocked credential file access: ${base}`;
  if (/(^|\/)\.codex(\/|$)/i.test(norm)) return "blocked access to ~/.codex";
  if (/(^|\/)\.config\/llm-router(\/|$)/i.test(norm)) return "blocked access to ~/.config/llm-router";
  return null;
}

// A path is checked as written (spaces kept: "a " and "a" are different files) and again after following symlinks.
export function forbiddenPath(raw, cwd) {
  const text = raw === undefined || raw === null ? "" : String(raw);
  if (!text.trim()) return null;
  const literal = deniedName(expandHome(text));
  if (literal) return literal;
  const real = resolveReal(text, cwd);
  return real === null ? `blocked path through a dangling or looping symlink: ${text}` : deniedName(real);
}

// One glob segment as an anchored regex: * ? [..] [!..] {a,b}; dotfiles are matched like any name.
export function globRegex(segment) {
  let out = "";
  for (let i = 0; i < segment.length; i++) {
    const ch = segment[i];
    if (ch === "*") out += "[^/]*";
    else if (ch === "?") out += "[^/]";
    else if (ch === "[") {
      const end = segment.indexOf("]", i + 2);
      if (end < 0) out += "\\[";
      else {
        let body = segment.slice(i + 1, end);
        const negate = body[0] === "!" || body[0] === "^";
        if (negate) body = body.slice(1);
        out += `[${negate ? "^" : ""}${body.replace(/[\\\]^]/g, "\\$&")}]`;
        i = end;
      }
    } else if (ch === "{") {
      const end = segment.indexOf("}", i + 1);
      if (end < 0) out += "\\{";
      else {
        const alternatives = segment.slice(i + 1, end).split(",").map((alt) => globRegex(alt).source.slice(1, -1));
        out += `(?:${alternatives.join("|")})`;
        i = end;
      }
    } else out += ch.replace(/[.*+?^${}()|[\]\\/]/g, "\\$&");
  }
  return new RegExp(`^${out}$`, "i");
}

// A glob or search pattern that names a secret: a segment mentioning .env (not .env.example), .p8, auth.json or
// .codex; a dot-segment whose glob can match .env/.env.local/.codex; or .config followed by a match for llm-router.
export function forbiddenGlob(raw) {
  const text = expandHome(String(raw ?? "").trim()).toLowerCase().replace(/\\/g, "/");
  if (!text) return null;
  if (/\.config\/llm-router/.test(text)) return "blocked pattern naming ~/.config/llm-router";
  const segments = text.split("/");
  for (let k = 0; k < segments.length; k++) {
    const segment = segments[k];
    if (segment.includes(".env") && segment !== ".env.example") return `blocked pattern naming secret files: ${raw}`;
    if (segment.includes(".p8") || segment.includes("auth.json")) return `blocked pattern naming credential files: ${raw}`;
    if (segment.includes(".codex")) return "blocked pattern naming ~/.codex";
    if (segment.startsWith(".") && GLOB_CHARS.test(segment)) {
      const rx = globRegex(segment);
      if (DOT_SECRETS.some((name) => rx.test(name))) return `blocked pattern that matches secret files: ${raw}`;
      if (rx.test(".config") && segments[k + 1] && globRegex(segments[k + 1]).test("llm-router")) {
        return "blocked pattern that matches ~/.config/llm-router";
      }
    }
  }
  return null;
}

// Directories under root, root included, without following symlinks; null past GLOB_LIMIT.
function descendants(root) {
  const found = [root];
  for (let k = 0; k < found.length; k++) {
    let entries = [];
    try {
      entries = fs.readdirSync(found[k], { withFileTypes: true });
    } catch {}
    for (const entry of entries) {
      if (entry.isDirectory()) found.push(path.join(found[k], entry.name));
      if (found.length > GLOB_LIMIT) return null;
    }
  }
  return found;
}

// Expand a glob against the filesystem segment by segment ("**" = any depth); {overflow: true} past GLOB_LIMIT.
export function expandGlob(word, cwd) {
  const expanded = expandHome(String(word));
  let current = [path.isAbsolute(expanded) ? "/" : path.resolve(String(cwd))];
  for (const segment of expanded.split("/")) {
    if (!segment || segment === ".") continue;
    if (segment === "..") current = current.map((p) => path.dirname(p));
    else if (segment === "**") {
      const next = [];
      for (const dir of current) {
        const below = descendants(dir);
        if (below === null) return { overflow: true, matches: [] };
        next.push(...below);
        if (next.length > GLOB_LIMIT) return { overflow: true, matches: [] };
      }
      current = next;
    } else if (!GLOB_CHARS.test(segment)) current = current.map((p) => path.join(p, segment));
    else {
      const rx = globRegex(segment);
      const next = [];
      for (const dir of current) {
        let names = [];
        try {
          names = fs.readdirSync(dir);
        } catch {}
        for (const name of names) if (rx.test(name)) next.push(path.join(dir, name));
        if (next.length > GLOB_LIMIT) return { overflow: true, matches: [] };
      }
      current = next;
    }
  }
  return { overflow: false, matches: current };
}

function forbiddenWildcard(word, cwd) {
  if (!GLOB_CHARS.test(word)) return null;
  const named = forbiddenGlob(word);
  if (named) return named;
  const { overflow, matches } = expandGlob(word, cwd);
  if (overflow) return `blocked glob too broad to check: ${word}`;
  for (const match of matches) {
    const why = forbiddenPath(match, cwd);
    if (why) return why;
  }
  return null;
}

// A search rooted at an ancestor of a secret directory (for example $HOME) reads it too.
function searchRootCovering(root) {
  for (const secret of [path.join(home(), ".codex"), path.join(home(), ".config", "llm-router")]) {
    const real = resolveReal(secret, "/");
    for (const candidate of new Set([secret, real ?? secret])) {
      if (candidate === root || candidate.startsWith(root.endsWith(path.sep) ? root : root + path.sep)) {
        return `blocked search rooted at ${root}: it covers ${candidate}`;
      }
    }
  }
  return null;
}

// A recursive read of raw (grep -r, rg, find, the Grep tool) reaches a secret: the root covers ~/.codex or
// ~/.config/llm-router, or a walk (not following symlinks, checking where they point) meets a secret name.
export function treeReachesSecret(raw, cwd) {
  const root = resolveReal(raw, cwd);
  if (root === null) return `blocked recursive read through a dangling or looping symlink: ${raw}`;
  const direct = deniedName(root) || searchRootCovering(root);
  if (direct) return direct;
  let seen = 0;
  const stack = [root];
  while (stack.length) {
    const dir = stack.pop();
    let entries = [];
    try {
      entries = fs.readdirSync(dir, { withFileTypes: true });
    } catch {}
    for (const entry of entries) {
      if (++seen > SEARCH_LIMIT) return `blocked recursive read too large to check: ${raw} (narrow the path)`;
      const full = path.join(dir, entry.name);
      const named = deniedName(full);
      if (named) return `${named} (inside ${raw})`;
      if (entry.isSymbolicLink()) {
        let real;
        try {
          real = fs.realpathSync(full);
        } catch {
          continue;  // a dangling link reads nothing
        }
        const why = deniedName(real) || searchRootCovering(real);
        if (why) return `${why} (via ${full})`;
      } else if (entry.isDirectory()) stack.push(full);  // .git too: grep -r reads it, a planted link can hide there
    }
  }
  return null;
}

// ---- shell parsing ----------------------------------------------------------------------------------------
function ansiC(body) {
  const named = { n: "\n", t: "\t", r: "\r", a: "\x07", b: "\b", e: "\x1b", E: "\x1b", f: "\f", v: "\v",
    "\\": "\\", "'": "'", '"': '"', "?": "?" };
  let out = "";
  for (let j = 0; j < body.length; j++) {
    if (body[j] !== "\\") {
      out += body[j];
      continue;
    }
    const n = body[++j];
    const hex = (max) => new RegExp(`^[0-9a-fA-F]{1,${max}}`).exec(body.slice(j + 1));
    if (n === undefined) out += "\\";
    else if (n in named) out += named[n];
    else if (n === "x" || n === "u" || n === "U") {
      const m = hex(n === "x" ? 2 : n === "u" ? 4 : 8);
      if (m) {
        out += String.fromCodePoint(parseInt(m[0], 16));
        j += m[0].length;
      } else out += "\\" + n;
    } else if (/[0-7]/.test(n)) {
      const m = /^[0-7]{1,3}/.exec(body.slice(j));
      out += String.fromCharCode(parseInt(m[0], 8));
      j += m[0].length - 1;
    } else if (n === "c" && body[j + 1] !== undefined) out += String.fromCharCode(body[++j].toUpperCase().charCodeAt(0) ^ 64);
    else out += "\\" + n;
  }
  return out;
}

// Splits a command line into simple commands (arrays of unquoted words). Separators: newline ; & | ( ) < >.
// Braces stay in the word (brace expansion is checked by rm/glob rules). Line continuations are joined, quotes,
// $'...' (ANSI-C, decoded) and $"..." and backslashes removed. $(...), `...` and <(...) bodies are returned as nested
// scripts and leave a "$" in the word, so a target built from them is never treated as resolvable.
export function parseShell(script) {
  const commands = [];
  const nested = [];
  let words = [];
  let word = null;
  const src = String(script ?? "").replace(/\\\r?\n/g, "");
  const endWord = () => {
    if (word !== null) words.push(word);
    word = null;
  };
  const piped = [];  // piped[n]: a "|" came anywhere before commands[n]
  let pipeSeen = false;
  const endCommand = (separator) => {
    endWord();
    if (words.length) {
      commands.push(words);
      piped.push(pipeSeen);
    }
    words = [];
    if (separator === "|") pipeSeen = true;
  };
  const add = (text) => {
    word = (word ?? "") + text;
  };
  const substitution = (start) => {  // src[start] opens "(" of $( or <(; returns index after the matching ")"
    let depth = 0;
    for (let j = start; j < src.length; j++) {
      if (src[j] === "(") depth++;
      else if (src[j] === ")" && --depth === 0) {
        nested.push(src.slice(start + 1, j));
        return j + 1;
      }
    }
    nested.push(src.slice(start + 1));
    return src.length;
  };
  const backtick = (start) => {
    const end = src.indexOf("`", start + 1);
    nested.push(src.slice(start + 1, end < 0 ? src.length : end));
    return end < 0 ? src.length : end + 1;
  };
  let i = 0;
  while (i < src.length) {
    const ch = src[i];
    if (ch === "$" && src[i + 1] === "'") {
      let j = i + 2;
      while (j < src.length && src[j] !== "'") j += src[j] === "\\" ? 2 : 1;
      add(ansiC(src.slice(i + 2, Math.min(j, src.length))));
      i = j + 1;
    } else if (ch === "$" && src[i + 1] === '"') {
      i++;  // $"..." is a double-quoted string
    } else if (ch === "\\") {
      add(src[i + 1] ?? "");
      i += 2;
    } else if (ch === "'") {
      const end = src.indexOf("'", i + 1);
      add(src.slice(i + 1, end < 0 ? src.length : end));
      i = end < 0 ? src.length : end + 1;
    } else if (ch === '"') {
      i++;
      add("");
      while (i < src.length && src[i] !== '"') {
        if (src[i] === "\\" && i + 1 < src.length) {
          add(src[i + 1]);
          i += 2;
        } else if (src[i] === "$" && src[i + 1] === "(") {
          add("$");
          i = substitution(i + 1);
        } else if (src[i] === "`") {
          add("$");
          i = backtick(i);
        } else {
          add(src[i]);
          i++;
        }
      }
      i++;
    } else if ((ch === "$" || ch === "<") && src[i + 1] === "(") {
      add("$");
      i = substitution(i + 1);
    } else if (ch === "`") {
      add("$");
      i = backtick(i);
    } else if (ch === " " || ch === "\t") {
      endWord();
      i++;
    } else if (ch === "|" && src[i + 1] === "|") {
      endCommand("||");
      i += 2;
    } else if ("\n;&|()<>".includes(ch)) {
      endCommand(ch);
      i++;
    } else {
      add(ch);
      i++;
    }
  }
  endCommand();
  return { commands, nested, piped };
}

function stripPrefix(words) {
  let i = 0;
  for (;;) {
    const w = words[i];
    if (w === undefined) return [];
    if (/^[A-Za-z_][A-Za-z0-9_]*=/.test(w)) i++;
    else if (WRAPPERS.has(path.basename(w))) {
      i++;
      while (words[i] !== undefined && (words[i].startsWith("-") || /^[A-Za-z_][A-Za-z0-9_]*=/.test(words[i]))) i++;
    } else if (path.basename(w) === "timeout") i += 2;
    else return words.slice(i);
  }
}

// A recursive rm must name every target statically; each resolves against the real cwd and must stay inside the
// job root. Any argument built at runtime ($VAR, $(...), `...`) could be a recursive flag or target: blocked.
function checkRm(args, root, cwd, moved) {
  const dynamic = args.find((a) => /[$`]/.test(a));
  if (dynamic !== undefined) return `blocked rm with an argument built at runtime: ${dynamic}`;
  let recursive = false;
  let endOptions = false;
  const targets = [];
  for (const arg of args) {
    if (!endOptions && arg === "--") endOptions = true;
    else if (!endOptions && arg.startsWith("--")) recursive ||= arg === "--recursive";
    else if (!endOptions && arg.startsWith("-") && arg.length > 1) recursive ||= /[rR]/.test(arg.slice(1));
    else targets.push(arg);
  }
  if (!recursive) return null;
  if (!targets.length) return "blocked recursive rm whose targets come at runtime (list explicit paths)";
  const wt = root ? resolveReal(root, "/") : null;
  if (!wt) return "blocked recursive rm without a known job root";
  for (const target of targets) {
    if (/[{}]/.test(target)) return `blocked recursive rm with brace expansion: ${target} (list explicit paths)`;
    if (target.startsWith("~") && target !== "~" && !target.startsWith("~/")) return `blocked recursive rm of ${target}`;
    if (moved && !path.isAbsolute(expandHome(target))) return `blocked recursive rm of a relative target after cd: ${target}`;
    const segments = target.split("/");
    const globbed = GLOB_CHARS.test(target);
    if (globbed && (segments.includes("..") || segments.slice(0, -1).some((s) => GLOB_CHARS.test(s)))) {
      return `blocked recursive rm of a glob that can leave its directory: ${target} (list explicit paths)`;
    }
    const fixed = globbed ? segments.slice(0, -1).join("/") || (target.startsWith("/") ? "/" : ".") : target;
    const real = resolveReal(fixed, cwd);
    if (real === null) return `blocked recursive rm through a dangling or looping symlink: ${target}`;
    const inside = real.startsWith(wt + path.sep) || (globbed && real === wt);
    if (!inside) return `blocked rm -rf outside worktree: ${target}`;
  }
  return null;
}

// Parse a reader's arguments against its spec. {bad} names the first option the guard does not fully understand;
// otherwise {recursive, paths}. Only fully spelled, listed options pass, so no spelling can hide recursion or a path.
export function parseReader(name, args) {
  if (name === "find") return parseFind(args);
  const spec = READERS[name];
  const plain = [];
  let explicit = false;
  let recursive = spec.always;
  for (let k = 0; k < args.length; k++) {
    const arg = args[k];
    if (arg === "--") {
      plain.push(...args.slice(k + 1));
      break;
    }
    if (arg.startsWith("--")) {
      const eq = arg.indexOf("=");
      const flag = eq < 0 ? arg : arg.slice(0, eq);
      if (spec.longValue.includes(flag)) {
        if (eq < 0) k++;
      } else if (spec.optionalEq.includes(flag)) {
        // a bare flag or flag=value
      } else if (spec.long.includes(flag) && eq < 0) {
        recursive ||= spec.recursiveLong.includes(flag);
        continue;
      } else return { bad: arg };
      explicit ||= spec.explicitLong.includes(flag);
    } else if (arg.startsWith("-") && arg.length > 1) {
      for (let c = 1; c < arg.length; c++) {
        const letter = arg[c];
        if (spec.shortValue.includes(letter)) {
          explicit ||= spec.explicit.includes(letter);
          if (c === arg.length - 1) k++;  // the value is the next word, else the rest of the cluster
          break;
        }
        if (!spec.short.includes(letter)) return { bad: arg };
        recursive ||= spec.recursiveShort.includes(letter);
      }
    } else plain.push(arg);
  }
  const paths = spec.pattern && !explicit ? plain.slice(1) : plain;
  return { recursive, paths };
}

function parseFind(args) {
  let k = 0;
  const paths = [];
  while (k < args.length && (FIND_LEADING.has(args[k]) || /^-O\d$/.test(args[k]) || args[k] === "-f")) {
    if (args[k] === "-f") paths.push(args[k + 1] ?? ".");
    k += args[k] === "-f" ? 2 : 1;
  }
  for (; k < args.length && !(args[k].startsWith("-") || ["(", "!", ")", ","].includes(args[k])); k++) paths.push(args[k]);
  if (!paths.length) paths.push(".");
  for (; k < args.length; k++) {
    const arg = args[k];
    if (FIND_EXEC.has(arg)) {  // the executed command's own words run until ";" or "+"
      while (k + 1 < args.length && args[k + 1] !== ";" && args[k + 1] !== "+") k++;
      k++;
    } else if (arg.startsWith("-") && !FIND_PRIMARIES.has(arg)) return { bad: arg };
  }
  return { recursive: true, paths };
}

// One rule for grep/rg/ag/ack/find/tree: options the guard cannot fully parse are blocked; a recursive read fed by a
// pipe or a feeder (xargs, parallel, rush), or with an argument built at runtime, is blocked; every path it reads
// must not reach a secret.
function checkReader(name, args, cwd, fed) {
  const parsed = parseReader(name, args);
  if (parsed.bad !== undefined) {
    return `blocked ${name} with an option the guard cannot fully parse: ${parsed.bad} (spell options in full)`;
  }
  if (!parsed.recursive) return null;
  if (fed) return `blocked recursive ${name} fed by a pipe or ${[...FEEDERS].join("/")}: its paths come at runtime`;
  const dynamic = args.find((a) => /[$`]/.test(a));
  if (dynamic !== undefined) return `blocked recursive ${name} with an argument built at runtime: ${dynamic}`;
  for (const target of parsed.paths.length ? parsed.paths : ["."]) {
    const why = treeReachesSecret(target, cwd);
    if (why) return why;
  }
  return null;
}

function checkGit(name, args) {
  if (name === "gh") {
    if (args[0] === "pr" && args[1] === "merge") return "blocked gh pr merge in headless Claude job";
    if (args[0] === "pr" && args[1] === "review" && args.some((a) => a === "--approve" || a === "-a")) {
      return "blocked gh pr review --approve in headless Claude job";
    }
    return null;
  }
  let i = 0;
  while (i < args.length && args[i].startsWith("-")) i += ["-C", "-c", "--git-dir", "--work-tree", "--namespace"].includes(args[i]) ? 2 : 1;
  const sub = args[i];
  const rest = args.slice(i + 1);
  if (sub === "push") return "blocked git push in headless Claude job";
  if (sub === "reset" && rest.includes("--hard")) return "blocked git reset --hard in headless Claude job";
  if (sub === "merge" && rest.some((a) => /\b(?:main|master)\b/.test(a))) return "blocked git merge into main/master";
  return null;
}

function checkScript(script, root, cwd, depth = 0) {
  if (depth > 8) return "blocked deeply nested shell command";
  if (FORBIDDEN_GIT.test(String(script).replace(/\\\r?\n/g, " ").replace(/\s+/g, " "))) {
    return "blocked destructive git/gh command in headless Claude job";
  }
  const { commands, nested, piped } = parseShell(script);
  for (const inner of nested) {
    const why = checkScript(inner, root, cwd, depth + 1);
    if (why) return why;
  }
  let moved = false;  // after cd/pushd/popd a relative path no longer resolves against the hook's cwd
  for (const [index, raw] of commands.entries()) {
    for (const word of raw) {
      for (const piece of word.split("=")) {
        const why = forbiddenPath(piece, cwd) || forbiddenWildcard(piece, cwd);
        if (why) return why;
      }
    }
    // Wrappers and keywords can put a command anywhere in the word list (nice -n 5 rm, then rm, xargs rm, ! rm),
    // so shells, eval, rm and recursive readers are recognised at every position, not only the first.
    for (let k = 0; k < raw.length; k++) {
      const name = path.basename(raw[k]);
      const args = raw.slice(k + 1);
      let why = null;
      if (SHELLS.has(name)) {
        const flag = args.findIndex((a) => /^-[A-Za-z]*c[A-Za-z]*$/.test(a));
        if (flag >= 0 && args[flag + 1] !== undefined) why = checkScript(args[flag + 1], root, cwd, depth + 1);
      } else if (name === "eval") why = checkScript(args.join(" "), root, cwd, depth + 1);
      else if (name === "rm") why = checkRm(args, root, cwd, moved);
      else if (name in READERS || name === "find") {
        why = checkReader(name, args, cwd, piped[index] || raw.slice(0, k).some((w) => FEEDERS.has(path.basename(w))));
      }
      if (why) return why;
    }
    const words = stripPrefix(raw);
    if (!words.length) continue;
    const name = path.basename(words[0]);
    const why = name === "git" || name === "gh" ? checkGit(name, words.slice(1)) : null;
    if (why) return why;
    moved ||= ["cd", "pushd", "popd"].includes(name);
  }
  return null;
}

// root: the job root containment is checked against (null = unknown). cwd: where relative paths resolve (the real
// cwd of the tool call); defaults to root.
export function evaluateEvent(event, root, cwd) {
  if (!event || typeof event !== "object" || Array.isArray(event)) return { allow: false, reason: "malformed hook event" };
  if (typeof event.tool_name !== "string" || !event.tool_name.trim()) {
    return { allow: false, reason: "malformed hook event: no tool_name" };
  }
  const input = event.tool_input;
  if (!input || typeof input !== "object" || Array.isArray(input)) {
    return { allow: false, reason: "malformed hook event: tool_input is not an object" };
  }
  const toolName = event.tool_name.toLowerCase();
  const base = cwd || root || process.cwd();
  for (const key of FILE_KEYS) {
    const why = forbiddenPath(input[key], base);
    if (why) return { allow: false, reason: why };
  }
  if (SEARCH_TOOLS.has(toolName) || "glob" in input) {
    const searchRoot = resolveReal(String(input.path || "."), base);
    if (searchRoot === null) return { allow: false, reason: "blocked search through a dangling or looping symlink" };
    let why = searchRootCovering(searchRoot);
    // ripgrep applies a Grep glob without "/" at any depth, so it is checked as **/<glob>
    const grepGlob = input.glob ? (String(input.glob).includes("/") ? String(input.glob) : `**/${input.glob}`) : undefined;
    const patterns = [grepGlob, toolName === "glob" ? input.pattern : undefined].filter(Boolean).map(String);
    for (const pattern of patterns) {
      why ||= GLOB_CHARS.test(pattern) ? forbiddenWildcard(pattern, searchRoot) : forbiddenPath(pattern, searchRoot);
    }
    if (!why && toolName === "glob" && input.pattern) {
      why = forbiddenPath(String(input.pattern).split(GLOB_CHARS)[0] || ".", searchRoot);
    }
    // the Grep tool always walks: its glob syntax is ripgrep's, which the guard does not fully model
    if (!why && toolName === "grep") why = treeReachesSecret(searchRoot, base);
    if (why) return { allow: false, reason: why };
  }
  if (toolName === "bash" || typeof input.command === "string") {
    const why = checkScript(input.command, root, base);
    if (why) return { allow: false, reason: why };
  }
  return { allow: true, reason: null };
}
