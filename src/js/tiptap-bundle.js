/**
 * Tiptap Editor Bundle for MoA Document Converter
 *
 * This file is the esbuild entry point. It bundles all Tiptap extensions
 * and helpers into a single ES module (tiptap.bundle.js) that can be
 * imported by the editor page without a framework.
 *
 * Build: cd src && npm run build
 */

import { Editor } from "@tiptap/core";
import StarterKit from "@tiptap/starter-kit";
import Table from "@tiptap/extension-table";
import TableRow from "@tiptap/extension-table-row";
import TableCell from "@tiptap/extension-table-cell";
import TableHeader from "@tiptap/extension-table-header";
import Underline from "@tiptap/extension-underline";
import TextAlign from "@tiptap/extension-text-align";
import Image from "@tiptap/extension-image";
import Placeholder from "@tiptap/extension-placeholder";
import TurndownService from "turndown";
import { marked } from "marked";

// ─── Turndown (HTML → Markdown) ─────────────────────────
const turndown = new TurndownService({
  headingStyle: "atx",
  hr: "---",
  bulletListMarker: "-",
  codeBlockStyle: "fenced",
});

// Add table support to Turndown
turndown.addRule("tableCell", {
  filter: ["th", "td"],
  replacement: (content) => ` ${content.trim()} |`,
});
turndown.addRule("tableRow", {
  filter: "tr",
  replacement: (content) => `|${content}\n`,
});
turndown.addRule("table", {
  filter: "table",
  replacement: (content) => {
    // Add header separator row after first row
    const rows = content.trim().split("\n").filter((r) => r);
    if (rows.length > 0) {
      const firstRow = rows[0];
      const cols = firstRow.split("|").filter((c) => c).length;
      const separator = "|" + " --- |".repeat(cols);
      rows.splice(1, 0, separator);
    }
    return "\n" + rows.join("\n") + "\n\n";
  },
});

/**
 * Create a Tiptap editor instance configured for MoA document editing.
 *
 * @param {HTMLElement} element - The DOM element to attach the editor to
 * @param {object} options - Configuration options
 * @param {string} options.content - Initial content (HTML or empty)
 * @param {function} options.onUpdate - Called on every content change
 * @returns {Editor} Tiptap Editor instance
 */
function createMoaEditor(element, options = {}) {
  const editor = new Editor({
    element,
    extensions: [
      StarterKit.configure({
        heading: { levels: [1, 2, 3, 4, 5, 6] },
      }),
      Table.configure({
        resizable: true,
        HTMLAttributes: { class: "moa-table" },
      }),
      TableRow,
      TableCell,
      TableHeader,
      Underline,
      TextAlign.configure({
        types: ["heading", "paragraph"],
        alignments: ["left", "center", "right", "justify"],
      }),
      Image.configure({
        inline: false,
        allowBase64: true,
      }),
      Placeholder.configure({
        placeholder: options.placeholder || "문서 내용을 편집하세요...",
      }),
    ],
    content: options.content || "",
    editable: options.editable !== false,
    autofocus: options.autofocus || false,
    onUpdate: options.onUpdate || undefined,
  });

  return editor;
}

/**
 * Convert HTML to Markdown using Turndown.
 */
function htmlToMarkdown(html) {
  return turndown.turndown(html);
}

/**
 * Convert Markdown to HTML using marked.
 */
function markdownToHtml(md) {
  return marked.parse(md);
}

/**
 * Load HTML content into a Tiptap editor.
 */
function setEditorContent(editor, html) {
  editor.commands.setContent(html);
}

/**
 * Get the editor content as HTML.
 */
function getEditorHtml(editor) {
  return editor.getHTML();
}

/**
 * Get the editor content as Markdown.
 */
function getEditorMarkdown(editor) {
  const html = editor.getHTML();
  return htmlToMarkdown(html);
}

/**
 * Get the editor content as JSON.
 */
function getEditorJson(editor) {
  return editor.getJSON();
}

// Export everything the editor page needs
export {
  createMoaEditor,
  htmlToMarkdown,
  markdownToHtml,
  setEditorContent,
  getEditorHtml,
  getEditorMarkdown,
  getEditorJson,
  Editor,
};
