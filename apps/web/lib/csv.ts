/**
 * Minimal CSV generation + client-side download. No existing precedent in
 * this repo (checked) — this is a from-scratch, dependency-free helper: the
 * export surfaces here are small and fixed-shape enough that a full CSV
 * library would be overkill.
 */

function escapeCsvCell(value: string): string {
  if (/[",\n]/.test(value)) {
    return `"${value.replace(/"/g, '""')}"`;
  }
  return value;
}

/** Turn an array of flat objects into a CSV string (header row + rows). */
export function toCsv(rows: Record<string, string>[]): string {
  if (rows.length === 0) return "";
  const headers = Object.keys(rows[0]!);
  const lines = [
    headers.map(escapeCsvCell).join(","),
    ...rows.map((row) =>
      headers.map((h) => escapeCsvCell(row[h] ?? "")).join(","),
    ),
  ];
  return lines.join("\r\n");
}

/** Trigger a browser download of `content` as `filename`. Client-side only. */
export function downloadCsv(filename: string, content: string): void {
  const blob = new Blob([content], { type: "text/csv;charset=utf-8;" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}
