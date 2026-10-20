/**
 * A small Markdown renderer for agent replies.
 *
 * The spec requires Markdown, and the obvious move is a library. This is not a library, and
 * the reasons are worth stating because "just add `marked`" is a reasonable instinct:
 *
 * 1. **This project has no runtime dependencies but React.** That is a deliberate property —
 *    no router, no state library, no CSS framework — and it is what keeps the bundle at 180 kB
 *    and the supply chain at zero. A Markdown parser is the largest thing anyone has proposed
 *    adding to it.
 * 2. **A general parser is a security surface.** `marked` needs a sanitizer, the sanitizer has
 *    its own CVEs, and the pairing has to be kept in step. Here, nothing is ever parsed into
 *    HTML: the input is escaped and the output is React elements, so there is no `innerHTML`
 *    anywhere and injection is not a thing that can happen — not because it is filtered, but
 *    because there is no path from text to markup.
 * 3. **The spec names the subset it wants**: paragraphs, lists, bold, code blocks, links.
 *    A renderer for exactly that list is ~150 readable lines. A renderer for *all* of Markdown
 *    is thousands, and the extra 95% would never be exercised by an agent's replies.
 *
 * What it handles: fenced code blocks, ATX headings, unordered and ordered lists, blockquotes,
 * horizontal rules, paragraphs, and inline `code`, **bold**, *italic*, and [links](url).
 * What it deliberately does not: tables, footnotes, nested lists beyond one level, inline HTML
 * (escaped, not rendered), and reference-style links. Unknown syntax degrades to plain text,
 * which is the correct failure for a renderer: showing the raw source of something you did not
 * understand is honest, and silently dropping it is not.
 *
 * URLs are restricted to `http`, `https` and `mailto`. An `href` is the one place a renderer
 * can be talked into executing something, so it is the one place that checks.
 */

import { Fragment, type ReactNode } from "react";

/* -------------------------------------------------------------------------- */
/* inline                                                                      */
/* -------------------------------------------------------------------------- */

/** Only these schemes become links; anything else renders as literal text. */
const SAFE_SCHEME = /^(https?:|mailto:)/i;

type Inline =
  | { kind: "text"; text: string }
  | { kind: "code"; text: string }
  | { kind: "bold"; text: string }
  | { kind: "italic"; text: string }
  | { kind: "link"; text: string; href: string };

/**
 * Tokenise inline spans.
 *
 * Ordered so that `code` wins over emphasis: inside backticks, `*` and `_` are literal, and a
 * parser that applied emphasis first would mangle `` `a_b_c` ``. This is the one ordering
 * decision in the file that matters.
 */
function parseInline(source: string): Inline[] {
  const out: Inline[] = [];
  let buffer = "";
  let i = 0;

  const flush = () => {
    if (buffer) {
      out.push({ kind: "text", text: buffer });
      buffer = "";
    }
  };

  while (i < source.length) {
    const rest = source.slice(i);

    // `code` — highest precedence, no nesting inside.
    const code = /^`([^`]+)`/.exec(rest);
    if (code) {
      flush();
      out.push({ kind: "code", text: code[1] });
      i += code[0].length;
      continue;
    }

    // **bold** before *italic*, or the italic rule eats the first asterisk of a pair.
    const bold = /^\*\*([^*]+)\*\*/.exec(rest);
    if (bold) {
      flush();
      out.push({ kind: "bold", text: bold[1] });
      i += bold[0].length;
      continue;
    }

    const italic = /^[*_]([^*_\n]+)[*_]/.exec(rest);
    if (italic) {
      flush();
      out.push({ kind: "italic", text: italic[1] });
      i += italic[0].length;
      continue;
    }

    // [text](url)
    const link = /^\[([^\]]+)\]\(([^)\s]+)\)/.exec(rest);
    if (link) {
      flush();
      const href = link[2];
      if (SAFE_SCHEME.test(href)) {
        out.push({ kind: "link", text: link[1], href });
      } else {
        // Not a link we will emit. Kept as text so nothing silently disappears — a
        // `javascript:` URL renders as the characters the author typed, which is both
        // harmless and a visible signal that it was not treated as a link.
        out.push({ kind: "text", text: link[0] });
      }
      i += link[0].length;
      continue;
    }

    buffer += source[i];
    i += 1;
  }

  flush();
  return out;
}

/** Render inline spans. Text is a React child, so it is escaped by React, not by us. */
function renderInline(source: string, keyPrefix: string): ReactNode[] {
  return parseInline(source).map((span, index) => {
    const key = `${keyPrefix}-${index}`;
    switch (span.kind) {
      case "code":
        return (
          <code key={key} className="md__code">
            {span.text}
          </code>
        );
      case "bold":
        return (
          <strong key={key} className="md__strong">
            {renderInline(span.text, key)}
          </strong>
        );
      case "italic":
        return (
          <em key={key} className="md__em">
            {renderInline(span.text, key)}
          </em>
        );
      case "link":
        return (
          <a
            key={key}
            className="md__link"
            href={span.href}
            target="_blank"
            // `noopener` matters even for a personal tool: without it the opened page gets a
            // handle on this window.
            rel="noopener noreferrer"
          >
            {span.text}
          </a>
        );
      default:
        return <Fragment key={key}>{span.text}</Fragment>;
    }
  });
}

/* -------------------------------------------------------------------------- */
/* blocks                                                                      */
/* -------------------------------------------------------------------------- */

type Block =
  | { kind: "p"; lines: string[] }
  | { kind: "h"; level: number; text: string }
  | { kind: "ul"; items: string[] }
  | { kind: "ol"; items: string[] }
  | { kind: "quote"; lines: string[] }
  | { kind: "code"; lang: string; text: string }
  | { kind: "hr" };

function parseBlocks(source: string): Block[] {
  const lines = source.replace(/\r\n?/g, "\n").split("\n");
  const blocks: Block[] = [];
  let paragraph: string[] = [];

  const flushParagraph = () => {
    if (paragraph.length) {
      blocks.push({ kind: "p", lines: paragraph });
      paragraph = [];
    }
  };

  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i];

    // Fenced code — consumed whole, so nothing inside is interpreted.
    const fence = /^\s*```\s*([\w+-]*)\s*$/.exec(line);
    if (fence) {
      flushParagraph();
      const lang = fence[1];
      const body: string[] = [];
      i += 1;
      while (i < lines.length && !/^\s*```\s*$/.test(lines[i])) {
        body.push(lines[i]);
        i += 1;
      }
      // An unterminated fence is normal while streaming — the closing ``` has not arrived
      // yet. Rendering what there is beats waiting, which would make the reply look stalled.
      blocks.push({ kind: "code", lang, text: body.join("\n") });
      continue;
    }

    if (!line.trim()) {
      flushParagraph();
      continue;
    }

    if (/^\s*(?:---+|\*\*\*+|___+)\s*$/.test(line)) {
      flushParagraph();
      blocks.push({ kind: "hr" });
      continue;
    }

    const heading = /^(#{1,6})\s+(.*)$/.exec(line);
    if (heading) {
      flushParagraph();
      blocks.push({ kind: "h", level: heading[1].length, text: heading[2] });
      continue;
    }

    if (/^\s*>\s?/.test(line)) {
      flushParagraph();
      const quote: string[] = [];
      while (i < lines.length && /^\s*>\s?/.test(lines[i])) {
        quote.push(lines[i].replace(/^\s*>\s?/, ""));
        i += 1;
      }
      i -= 1;
      blocks.push({ kind: "quote", lines: quote });
      continue;
    }

    if (/^\s*[-*+]\s+/.test(line)) {
      flushParagraph();
      const items: string[] = [];
      while (i < lines.length && /^\s*[-*+]\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^\s*[-*+]\s+/, ""));
        i += 1;
      }
      i -= 1;
      blocks.push({ kind: "ul", items });
      continue;
    }

    if (/^\s*\d+[.)]\s+/.test(line)) {
      flushParagraph();
      const items: string[] = [];
      while (i < lines.length && /^\s*\d+[.)]\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^\s*\d+[.)]\s+/, ""));
        i += 1;
      }
      i -= 1;
      blocks.push({ kind: "ol", items });
      continue;
    }

    paragraph.push(line);
  }

  flushParagraph();
  return blocks;
}

/**
 * Render agent prose as Markdown.
 *
 * Every returned node is a React element built from escaped text, so there is no markup
 * injection path. See the module docstring for why this is not a library.
 */
export function Markdown({ text }: { text: string }) {
  const blocks = parseBlocks(text);

  return (
    <div className="md">
      {blocks.map((block, index) => {
        const key = `b-${index}`;
        switch (block.kind) {
          case "code":
            return (
              <pre key={key} className="md__pre" data-lang={block.lang || undefined}>
                <code>{block.text}</code>
              </pre>
            );
          case "h":
            return (
              // Heading level is capped at h3 in the DOM: this is prose inside a bubble, and
              // an h1 inside a message would outrank the page's own headings in the outline.
              <p
                key={key}
                className="md__h"
                data-level={Math.min(block.level, 3)}
              >
                {renderInline(block.text, key)}
              </p>
            );
          case "ul":
            return (
              <ul key={key} className="md__ul">
                {block.items.map((item, j) => (
                  <li key={`${key}-${j}`}>{renderInline(item, `${key}-${j}`)}</li>
                ))}
              </ul>
            );
          case "ol":
            return (
              <ol key={key} className="md__ol">
                {block.items.map((item, j) => (
                  <li key={`${key}-${j}`}>{renderInline(item, `${key}-${j}`)}</li>
                ))}
              </ol>
            );
          case "quote":
            return (
              <blockquote key={key} className="md__quote">
                {renderInline(block.lines.join(" "), key)}
              </blockquote>
            );
          case "hr":
            return <hr key={key} className="md__hr" />;
          default:
            return (
              <p key={key} className="md__p">
                {renderInline(block.lines.join("\n"), key)}
              </p>
            );
        }
      })}
    </div>
  );
}
