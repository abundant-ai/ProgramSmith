import { useEffect, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import {
  ChevronDown,
  ChevronRight,
  Code2,
  FileJson,
  FileText,
  FileCode2,
  FileTerminal,
  Folder,
  FolderOpen,
  Image as ImageIcon,
  Loader2,
  X,
} from "lucide-react";
import { api, type FileNode, type FilePreview } from "../api";
import { compactNum } from "../lib/format";
import { cn } from "../lib/cn";

/* ---------------------------------------------------------------------------
 * File / directory viewer — a right-side drawer:
 * a navigable tree on the left, a type-aware preview on the right (markdown
 * rendered with a raw toggle, code with line numbers, images inline, binaries
 * flagged). All reads come from the run's working dir via the file-browser API.
 * ------------------------------------------------------------------------- */

const CODE_LANGS = new Set([
  "python", "rust", "typescript", "tsx", "javascript", "jsx", "go", "c", "cpp", "java",
  "ruby", "bash", "toml", "yaml", "json", "html", "css", "scss", "sql", "dockerfile",
  "makefile", "ini", "diff",
]);

function fileIcon(node: FileNode) {
  const lang = node.lang ?? "";
  if (node.name.endsWith(".md")) return FileText;
  if (lang === "json") return FileJson;
  if (lang === "bash" || lang === "dockerfile" || lang === "makefile") return FileTerminal;
  if (/\.(png|jpe?g|gif|webp|bmp|ico|svg|avif)$/i.test(node.name)) return ImageIcon;
  if (CODE_LANGS.has(lang)) return FileCode2;
  return Code2;
}

/* ---- tree -------------------------------------------------------------- */
function TreeNode({
  node,
  depth,
  selected,
  expanded,
  onToggle,
  onSelect,
}: {
  node: FileNode;
  depth: number;
  selected: string | null;
  expanded: Set<string>;
  onToggle: (path: string) => void;
  onSelect: (node: FileNode) => void;
}) {
  const isDir = node.type === "dir";
  const isOpen = expanded.has(node.path);
  const Icon = isDir ? (isOpen ? FolderOpen : Folder) : fileIcon(node);
  const isSel = selected === node.path;
  return (
    <div>
      <button
        onClick={() => (isDir ? onToggle(node.path) : onSelect(node))}
        className={cn(
          "focus-ring flex w-full items-center gap-1.5 rounded-md py-1 pr-2 text-left text-[13px] transition-colors",
          isSel ? "bg-accent-soft/30 text-ink" : "text-ink-2 hover:bg-surface-2",
        )}
        style={{ paddingLeft: `${depth * 12 + 6}px` }}
      >
        {isDir ? (
          isOpen ? (
            <ChevronDown className="size-3.5 shrink-0 text-ink-4" />
          ) : (
            <ChevronRight className="size-3.5 shrink-0 text-ink-4" />
          )
        ) : (
          <span className="w-3.5 shrink-0" />
        )}
        <Icon className={cn("size-3.5 shrink-0", isDir ? "text-accent" : "text-ink-4")} />
        <span className="truncate">{node.name}</span>
        {!isDir && node.size != null && (
          <span className="ml-auto shrink-0 pl-2 font-mono text-[10.5px] text-ink-4">
            {compactNum(node.size)}b
          </span>
        )}
      </button>
      {isDir && isOpen && node.children && (
        <div>
          {node.children.map((c) => (
            <TreeNode
              key={c.path}
              node={c}
              depth={depth + 1}
              selected={selected}
              expanded={expanded}
              onToggle={onToggle}
              onSelect={onSelect}
            />
          ))}
          {node.truncated && (
            <div
              className="py-1 text-[11px] italic text-ink-4"
              style={{ paddingLeft: `${(depth + 1) * 12 + 24}px` }}
            >
              … truncated
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/* ---- preview ----------------------------------------------------------- */
function CodeView({ content }: { content: string }) {
  const lines = content.replace(/\n$/, "").split("\n");
  return (
    <div className="overflow-auto rounded-lg border border-line bg-bg-2">
      <pre className="min-w-full text-[12.5px] leading-[1.6]">
        <code className="grid grid-cols-[auto_1fr] font-mono">
          {lines.map((ln, i) => (
            <div key={i} className="contents">
              <span className="select-none border-r border-line/60 px-3 text-right text-ink-4">
                {i + 1}
              </span>
              <span className="whitespace-pre px-3 text-ink-2">{ln || " "}</span>
            </div>
          ))}
        </code>
      </pre>
    </div>
  );
}

function Preview({ file }: { file: FilePreview }) {
  const [raw, setRaw] = useState(false);
  const isMd = file.lang === "markdown";
  useEffect(() => setRaw(false), [file.path]);

  if (file.kind === "image") {
    return (
      <div className="flex justify-center rounded-lg border border-line bg-bg-2 p-4">
        <img src={file.data_uri} alt={file.name} className="max-h-[70vh] max-w-full rounded" />
      </div>
    );
  }
  if (file.kind === "binary") {
    return <Empty label={`Binary file · ${compactNum(file.size)} bytes — no preview`} />;
  }
  if (file.kind === "too_large") {
    return <Empty label={`File too large to preview · ${compactNum(file.size)} bytes`} />;
  }
  const content = file.content ?? "";
  return (
    <div className="space-y-2">
      {isMd && (
        <div className="flex justify-end">
          <div className="inline-flex rounded-lg border border-line bg-surface p-0.5 text-[12px]">
            {(["rendered", "raw"] as const).map((m) => (
              <button
                key={m}
                onClick={() => setRaw(m === "raw")}
                className={cn(
                  "rounded-md px-2.5 py-1 font-medium capitalize transition-colors",
                  (m === "raw") === raw ? "bg-surface-3 text-ink" : "text-ink-3 hover:text-ink",
                )}
              >
                {m}
              </button>
            ))}
          </div>
        </div>
      )}
      {isMd && !raw ? <Markdown source={content} /> : <CodeView content={content} />}
    </div>
  );
}

function Empty({ label }: { label: string }) {
  return (
    <div className="flex h-40 items-center justify-center rounded-lg border border-dashed border-line text-[13px] text-ink-4">
      {label}
    </div>
  );
}

/* ---- minimal, safe markdown (React nodes; no dangerouslySetInnerHTML) --- */
function renderInline(text: string, keyBase: string): React.ReactNode[] {
  const nodes: React.ReactNode[] = [];
  // order matters: code spans first (so ** inside ` is literal), then links, bold, italic
  const re = /(`[^`]+`)|(\[[^\]]+\]\([^)]+\))|(\*\*[^*]+\*\*)|(\*[^*]+\*)|(_[^_]+_)/g;
  let last = 0;
  let m: RegExpExecArray | null;
  let i = 0;
  while ((m = re.exec(text))) {
    if (m.index > last) nodes.push(text.slice(last, m.index));
    const tok = m[0];
    const key = `${keyBase}-${i++}`;
    if (tok.startsWith("`")) {
      nodes.push(
        <code key={key} className="rounded bg-surface-2 px-1 py-0.5 font-mono text-[0.9em] text-accent">
          {tok.slice(1, -1)}
        </code>,
      );
    } else if (tok.startsWith("[")) {
      const mm = /\[([^\]]+)\]\(([^)]+)\)/.exec(tok)!;
      nodes.push(
        <a key={key} href={mm[2]} target="_blank" rel="noreferrer" className="text-accent hover:underline">
          {mm[1]}
        </a>,
      );
    } else if (tok.startsWith("**")) {
      nodes.push(<strong key={key} className="font-semibold text-ink">{tok.slice(2, -2)}</strong>);
    } else {
      nodes.push(<em key={key}>{tok.slice(1, -1)}</em>);
    }
    last = m.index + tok.length;
  }
  if (last < text.length) nodes.push(text.slice(last));
  return nodes;
}

function Markdown({ source }: { source: string }) {
  const blocks: React.ReactNode[] = [];
  const lines = source.split("\n");
  let i = 0;
  let key = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (line.startsWith("```")) {
      const buf: string[] = [];
      i++;
      while (i < lines.length && !lines[i].startsWith("```")) buf.push(lines[i++]);
      i++; // closing fence
      blocks.push(<CodeView key={key++} content={buf.join("\n")} />);
      continue;
    }
    const h = /^(#{1,6})\s+(.*)$/.exec(line);
    if (h) {
      const lvl = h[1].length;
      const cls = lvl <= 1 ? "text-lg" : lvl === 2 ? "text-base" : "text-sm";
      blocks.push(
        <p key={key++} className={cn("mt-3 font-semibold text-ink first:mt-0", cls)}>
          {renderInline(h[2], `h${key}`)}
        </p>,
      );
      i++;
      continue;
    }
    if (/^\s*([-*+])\s+/.test(line)) {
      const items: string[] = [];
      while (i < lines.length && /^\s*([-*+])\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^\s*([-*+])\s+/, ""));
        i++;
      }
      blocks.push(
        <ul key={key++} className="ml-5 list-disc space-y-1 text-[13px] text-ink-2">
          {items.map((it, k) => (
            <li key={k}>{renderInline(it, `li${key}-${k}`)}</li>
          ))}
        </ul>,
      );
      continue;
    }
    if (/^\s*\d+\.\s+/.test(line)) {
      const items: string[] = [];
      while (i < lines.length && /^\s*\d+\.\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^\s*\d+\.\s+/, ""));
        i++;
      }
      blocks.push(
        <ol key={key++} className="ml-5 list-decimal space-y-1 text-[13px] text-ink-2">
          {items.map((it, k) => (
            <li key={k}>{renderInline(it, `ol${key}-${k}`)}</li>
          ))}
        </ol>,
      );
      continue;
    }
    if (/^\s*>\s?/.test(line)) {
      blocks.push(
        <blockquote key={key++} className="border-l-2 border-line pl-3 text-[13px] italic text-ink-3">
          {renderInline(line.replace(/^\s*>\s?/, ""), `bq${key}`)}
        </blockquote>,
      );
      i++;
      continue;
    }
    if (/^\s*(-{3,}|\*{3,})\s*$/.test(line)) {
      blocks.push(<hr key={key++} className="border-line" />);
      i++;
      continue;
    }
    if (line.trim() === "") {
      i++;
      continue;
    }
    // paragraph: gather consecutive non-empty, non-special lines
    const para: string[] = [];
    while (
      i < lines.length &&
      lines[i].trim() !== "" &&
      !lines[i].startsWith("```") &&
      !/^(#{1,6})\s/.test(lines[i]) &&
      !/^\s*([-*+]|\d+\.)\s/.test(lines[i]) &&
      !/^\s*>\s?/.test(lines[i])
    ) {
      para.push(lines[i++]);
    }
    blocks.push(
      <p key={key++} className="text-[13px] leading-relaxed text-ink-2">
        {renderInline(para.join(" "), `p${key}`)}
      </p>,
    );
  }
  return <div className="space-y-2.5 rounded-lg border border-line bg-bg-2 px-4 py-3">{blocks}</div>;
}

/* ---- drawer ------------------------------------------------------------ */
function collectDefaultExpanded(root: FileNode): Set<string> {
  // auto-expand the run root, the generated task, and its single slug dir
  const open = new Set<string>([root.path]);
  const task = root.children?.find((c) => c.name === "task" && c.type === "dir");
  if (task) {
    open.add(task.path);
    if (task.children?.length === 1 && task.children[0].type === "dir") {
      open.add(task.children[0].path);
    }
  }
  return open;
}

function firstFile(node: FileNode): FileNode | null {
  if (node.type === "file") return node;
  for (const c of node.children ?? []) {
    const f = firstFile(c);
    if (f) return f;
  }
  return null;
}

function findByPath(node: FileNode, path: string): FileNode | null {
  if (node.path === path) return node.type === "file" ? node : null;
  for (const c of node.children ?? []) {
    const f = findByPath(c, path);
    if (f) return f;
  }
  return null;
}

function expandAncestors(base: Set<string>, path: string): Set<string> {
  const n = new Set(base);
  const parts = path.split("/");
  for (let i = 1; i < parts.length; i++) n.add(parts.slice(0, i).join("/"));
  return n;
}

export function FileExplorer({
  runKey,
  open,
  onClose,
  initialPath = null,
}: {
  runKey: string;
  open: boolean;
  onClose: () => void;
  /** When set, open this file on mount instead of the default instruction.md (e.g. a step prompt). */
  initialPath?: string | null;
}) {
  const [root, setRoot] = useState<FileNode | null>(null);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [selected, setSelected] = useState<string | null>(null);
  const [preview, setPreview] = useState<FilePreview | null>(null);
  const [loadingTree, setLoadingTree] = useState(false);
  const [loadingFile, setLoadingFile] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    document.body.style.overflow = "hidden";
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = "";
    };
  }, [open, onClose]);

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setLoadingTree(true);
    setError(null);
    api
      .listFiles(runKey)
      .then((r) => {
        if (cancelled) return;
        // Prefer an explicitly-requested file (e.g. a step's prompt), else the task's
        // instruction.md, else the first file.
        const requested = initialPath ? findByPath(r.tree, initialPath) : null;
        let exp = collectDefaultExpanded(r.tree);
        if (requested) exp = expandAncestors(exp, requested.path);
        setRoot(r.tree);
        setExpanded(exp);
        const task = r.tree.children?.find((c) => c.name === "task");
        const instr = task && firstFileNamed(task, "instruction.md");
        const target = requested ?? instr ?? firstFile(r.tree);
        if (target) void select(target);
      })
      .catch((e) => !cancelled && setError(String(e?.message ?? e)))
      .finally(() => !cancelled && setLoadingTree(false));
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, runKey, initialPath]);

  const select = async (node: FileNode) => {
    setSelected(node.path);
    setLoadingFile(true);
    try {
      setPreview(await api.readFile(runKey, node.path));
    } catch (e) {
      setPreview(null);
      setError(String((e as Error)?.message ?? e));
    } finally {
      setLoadingFile(false);
    }
  };

  const toggle = (path: string) =>
    setExpanded((prev) => {
      const n = new Set(prev);
      n.has(path) ? n.delete(path) : n.add(path);
      return n;
    });

  return (
    <AnimatePresence>
      {open && (
        <div className="fixed inset-0 z-50">
          <motion.div
            className="absolute inset-0 bg-bg-2/70 backdrop-blur-sm"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            onClick={onClose}
          />
          <motion.aside
            className="glass absolute inset-y-0 right-0 flex w-full max-w-[1040px] flex-col border-l border-line"
            initial={{ x: "100%" }}
            animate={{ x: 0 }}
            exit={{ x: "100%" }}
            transition={{ type: "spring", stiffness: 320, damping: 34 }}
          >
            <header className="flex items-center justify-between gap-3 border-b border-line px-5 py-3.5">
              <div className="flex items-center gap-2 text-[13px] font-semibold uppercase tracking-[0.08em] text-ink-3">
                <FolderOpen className="size-4 text-accent" />
                Files · <span className="font-mono normal-case text-ink-2">{runKey}</span>
              </div>
              <button
                onClick={onClose}
                className="focus-ring rounded-lg p-1.5 text-ink-3 transition-colors hover:bg-surface-2 hover:text-ink"
                aria-label="Close"
              >
                <X className="size-5" />
              </button>
            </header>

            <div className="grid min-h-0 flex-1 grid-cols-[280px_1fr]">
              {/* tree */}
              <div className="min-h-0 overflow-y-auto border-r border-line p-2">
                {loadingTree ? (
                  <div className="flex items-center gap-2 p-3 text-[13px] text-ink-4">
                    <Loader2 className="size-4 animate-spin" /> Loading tree…
                  </div>
                ) : root ? (
                  <TreeNode
                    node={root}
                    depth={0}
                    selected={selected}
                    expanded={expanded}
                    onToggle={toggle}
                    onSelect={(n) => void select(n)}
                  />
                ) : (
                  <p className="p-3 text-[13px] text-ink-4">{error ?? "No files."}</p>
                )}
              </div>

              {/* preview */}
              <div className="min-h-0 overflow-y-auto p-4">
                {selected && (
                  <div className="mb-3 flex items-center gap-2 text-[12.5px]">
                    <span className="truncate font-mono text-ink-2">{selected}</span>
                    {preview?.lang && (
                      <span className="rounded bg-surface-2 px-1.5 py-0.5 text-[11px] text-ink-3">
                        {preview.lang}
                      </span>
                    )}
                  </div>
                )}
                {loadingFile ? (
                  <div className="flex items-center gap-2 p-3 text-[13px] text-ink-4">
                    <Loader2 className="size-4 animate-spin" /> Loading…
                  </div>
                ) : preview ? (
                  <Preview file={preview} />
                ) : (
                  <Empty label="Select a file to preview." />
                )}
              </div>
            </div>
          </motion.aside>
        </div>
      )}
    </AnimatePresence>
  );
}

function firstFileNamed(node: FileNode, name: string): FileNode | null {
  if (node.type === "file") return node.name === name ? node : null;
  for (const c of node.children ?? []) {
    const f = firstFileNamed(c, name);
    if (f) return f;
  }
  return null;
}
