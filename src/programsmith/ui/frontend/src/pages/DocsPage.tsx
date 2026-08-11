import {
  useEffect,
  useState,
  type ComponentType,
  type ReactNode,
} from "react";
import { TerminalSquare } from "lucide-react";
import { Card, CardBody } from "../components/ui/Card";
import { cn } from "../lib/cn";

type IconType = ComponentType<{ className?: string }>;

interface Section {
  id: string;
  label: string;
  icon: IconType;
}
const SECTIONS: Section[] = [
  { id: "cli", label: "CLI reference", icon: TerminalSquare },
];

export function DocsPage() {
  const [active, setActive] = useState("cli");

  useEffect(() => {
    const onScroll = () => {
      let current = SECTIONS[0].id;
      for (const s of SECTIONS) {
        const el = document.getElementById(s.id);
        if (el && el.getBoundingClientRect().top <= 140) current = s.id;
      }
      setActive(current);
    };
    onScroll();
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, []);

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight text-ink">Documentation</h1>
      </div>

      <div className="grid gap-8 lg:grid-cols-[180px_1fr]">
        <nav className="top-24 hidden h-max lg:sticky lg:block">
          <ul className="space-y-0.5">
            {SECTIONS.map((s) => (
              <li key={s.id}>
                <a
                  href={`#${s.id}`}
                  className={cn(
                    "flex items-center gap-2 rounded-lg px-3 py-2 text-[13px] font-medium transition-colors",
                    active === s.id
                      ? "bg-surface-2 text-ink"
                      : "text-ink-3 hover:bg-surface-2/60 hover:text-ink-2",
                  )}
                >
                  <s.icon className="size-4" />
                  {s.label}
                </a>
              </li>
            ))}
          </ul>
        </nav>

        <div className="min-w-0 space-y-10">
          <Cli />
        </div>
      </div>
    </div>
  );
}

function Anchor({
  id,
  icon: Icon,
  title,
  children,
}: {
  id: string;
  icon: IconType;
  title: string;
  children: ReactNode;
}) {
  return (
    <section id={id} className="scroll-mt-24 space-y-4">
      <h2 className="flex items-center gap-2 text-lg font-semibold tracking-tight text-ink">
        <Icon className="size-5 text-ink-3" />
        {title}
      </h2>
      {children}
    </section>
  );
}

const CLI_GROUPS: Array<{ title: string; rows: Array<[string, string]> }> = [
  {
    title: "Create & farm",
    rows: [
      ["programsmith create --repo owner/name [--sha] [--slug]", "One repo → one calibrated task; starts and opens its local dashboard."],
      ["programsmith create --repo owner/name --draft", "Export after Static CI with no model sweeps or calibration."],
      ["programsmith farm --repos-file repos.txt", "Start and drive many runs from a file (one spec per line)."],
    ],
  },
  {
    title: "Runs & status",
    rows: [
      ["programsmith fleet [--json]", "List every run with stage, status, progress, and pass@1."],
      ["programsmith status <key> [--json]", "Full run detail: stage, sweeps, history."],
    ],
  },
  {
    title: "Gates & recovery",
    rows: [
      ["programsmith pick <key> --index N | --none", "Record the TASK MATRIX selection (human-gate mode only)."],
      ["programsmith qa-gate <key> --decision accept|revise|reject", "Record the final-gate decision (human-gate mode only)."],
      ["programsmith retry <key>", "Clear errored jobs so a blocked stage relaunches fresh."],
      ["programsmith reopen <key>", "Re-open a terminal run for another harden attempt."],
    ],
  },
  {
    title: "Serve & doctor",
    rows: [
      ["programsmith serve", "Start this dashboard in the background. Autodrive task generation is on; solver sweeps stay parked unless you pass --spend."],
      ["programsmith stop", "Stop the background dashboard explicitly."],
      ["programsmith doctor", "Preflight: Docker, credentials, disk, Claude Code CLI."],
    ],
  },
];

function Cli() {
  const [copied, setCopied] = useState<string | null>(null);

  return (
    <Anchor id="cli" icon={TerminalSquare} title="CLI reference">
      {CLI_GROUPS.map((g) => (
        <div key={g.title} className="space-y-2">
          <h3 className="text-[12px] font-semibold uppercase tracking-[0.08em] text-ink-4">
            {g.title}
          </h3>
          <Card>
            <CardBody className="divide-y divide-line/70 p-0">
              {g.rows.map(([cmd, desc]) => (
                <div
                  key={cmd}
                  className="grid gap-1 px-4 py-2.5 sm:grid-cols-[minmax(0,0.9fr)_1fr] sm:gap-4"
                >
                  <button
                    onClick={() => {
                      void navigator.clipboard?.writeText(cmd);
                      setCopied(cmd);
                      window.setTimeout(() => setCopied((c) => (c === cmd ? null : c)), 1200);
                    }}
                    className="focus-ring text-left font-mono text-[12.5px] text-ink hover:text-ink-2"
                    title="Copy"
                  >
                    {copied === cmd ? "copied ✓" : cmd}
                  </button>
                  <span className="text-[12.5px] text-ink-3">{desc}</span>
                </div>
              ))}
            </CardBody>
          </Card>
        </div>
      ))}
    </Anchor>
  );
}
