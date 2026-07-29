import { Fragment } from "react";

/**
 * Minimal, dependency-free markdown renderer for analyst proposal reports on the
 * CONFIG panel. Handles the subset the analyst emits — #/##/### headings, - / *
 * bullets, blank-line paragraphs, and inline **bold** / `code`. Not a full
 * CommonMark parser; anything fancier just renders as plain text (never raw HTML,
 * so proposal text can't inject markup).
 */

function renderInline(text: string, keyBase: string) {
  // Split on **bold** and `code`, keeping the delimiters.
  const parts = text.split(/(\*\*[^*]+\*\*|`[^`]+`)/g).filter(Boolean);
  return parts.map((p, i) => {
    if (p.startsWith("**") && p.endsWith("**")) {
      return (
        <strong key={`${keyBase}-${i}`} className="text-tape-text font-semibold">
          {p.slice(2, -2)}
        </strong>
      );
    }
    if (p.startsWith("`") && p.endsWith("`")) {
      return (
        <code
          key={`${keyBase}-${i}`}
          className="px-1 rounded bg-tape-panel-2 text-tape-accent text-[10.5px]"
        >
          {p.slice(1, -1)}
        </code>
      );
    }
    return <Fragment key={`${keyBase}-${i}`}>{p}</Fragment>;
  });
}

export default function MiniMarkdown({ text }: { text: string }) {
  const lines = text.replace(/\r\n/g, "\n").split("\n");
  const blocks: React.ReactNode[] = [];
  let bullets: string[] = [];
  let para: string[] = [];

  const flushPara = (k: string) => {
    if (para.length) {
      blocks.push(
        <p key={k} className="text-tape-sub leading-relaxed mb-2">
          {renderInline(para.join(" "), k)}
        </p>,
      );
      para = [];
    }
  };
  const flushBullets = (k: string) => {
    if (bullets.length) {
      blocks.push(
        <ul key={k} className="list-disc pl-5 mb-2 space-y-0.5">
          {bullets.map((b, i) => (
            <li key={`${k}-${i}`} className="text-tape-sub leading-relaxed">
              {renderInline(b, `${k}-${i}`)}
            </li>
          ))}
        </ul>,
      );
      bullets = [];
    }
  };

  lines.forEach((raw, idx) => {
    const line = raw.trimEnd();
    const k = `b${idx}`;
    const heading = /^(#{1,3})\s+(.*)$/.exec(line);
    const bullet = /^[-*]\s+(.*)$/.exec(line);
    if (heading) {
      flushPara(k);
      flushBullets(k);
      const level = heading[1].length;
      const cls =
        level === 1
          ? "text-tape-text font-bold text-[13px] tracking-[0.04em] mt-1 mb-1.5"
          : level === 2
          ? "text-tape-text font-semibold text-[12px] mt-2 mb-1"
          : "text-tape-sub font-semibold text-[11px] mt-1.5 mb-0.5";
      blocks.push(
        <div key={k} className={cls}>
          {renderInline(heading[2], k)}
        </div>,
      );
    } else if (bullet) {
      flushPara(k);
      bullets.push(bullet[1]);
    } else if (line === "") {
      flushPara(k);
      flushBullets(k);
    } else {
      flushBullets(k);
      para.push(line);
    }
  });
  flushPara("end");
  flushBullets("end");

  return <div className="tape-mono text-[11px]">{blocks}</div>;
}
