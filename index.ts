import { access, mkdir } from "node:fs/promises";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { type Static, StringEnum } from "@earendil-works/pi-ai";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

const SectorSchema = StringEnum(["general", "public", "private"] as const);

const FillPdfFormParamsSchema = Type.Object({
  pdf: Type.String({ description: "Absolute path to the input PDF form" }),
  data: Type.Optional(
    Type.String({
      description: "Absolute path to profile data JSON. Defaults to this extension's general_data file.",
    }),
  ),
  out: Type.Optional(
    Type.String({
      description: "Absolute output PDF path. Defaults to '<input stem> FILLED.pdf'.",
    }),
  ),
  sector: Type.Optional(SectorSchema),
  forceRemap: Type.Optional(
    Type.Boolean({
      description: "Rebuild the cached form layout and field mapping.",
    }),
  ),
  dryRun: Type.Optional(
    Type.Boolean({
      description: "Inspect mapping and counts without writing an output PDF.",
    }),
  ),
  useAiMapping: Type.Optional(
    Type.Boolean({
      description: "Use local Ollama to map unmapped fields from rich form context.",
    }),
  ),
  ollamaModel: Type.Optional(
    Type.String({
      description: "Ollama model used for AI mapping. Defaults to gemma4:e4b-it-q8_0.",
    }),
  ),
  minConfidence: Type.Optional(
    Type.Number({
      minimum: 0,
      maximum: 1,
      description: "Minimum AI mapping confidence to accept. Defaults to 0.86.",
    }),
  ),
});

type FillPdfFormParams = Static<typeof FillPdfFormParamsSchema>;

async function exists(path: string): Promise<boolean> {
  try {
    await access(path);
    return true;
  } catch {
    return false;
  }
}

async function ensurePython(pi: ExtensionAPI, extDir: string, signal: AbortSignal | undefined): Promise<string> {
  const venvDir = join(extDir, "venv");
  const pythonPath = join(venvDir, "bin", "python");
  if (await exists(pythonPath)) return pythonPath;

  await mkdir(venvDir, { recursive: true });
  await pi.exec("python3", ["-m", "venv", venvDir], { cwd: extDir, signal });
  await pi.exec(pythonPath, ["-m", "pip", "install", "--upgrade", "pip", "pypdf", "pymupdf"], { cwd: extDir, signal });
  return pythonPath;
}

function normalizePath(path: string): string {
  return resolve(path.replace(/^~/, process.env.HOME ?? "~"));
}

export default function registerPdfFormFiller(pi: ExtensionAPI) {
  const extDir = dirname(fileURLToPath(import.meta.url));

  pi.registerTool({
    name: "fill_pdf_form",
    label: "Fill PDF Form",
    description:
      "Fill an AcroForm PDF from structured profile data. It extracts form fields and visual widget layout, builds/caches a rich field schema, optionally asks local Ollama to map ambiguous fields, writes a filled PDF, and verifies the result.",
    promptSnippet:
      "fill_pdf_form: fill job-application or personal-data PDF forms from the configured general_data profile.",
    promptGuidelines: [
      "Use fill_pdf_form for PDF application forms that ask for personal, work, education, reference, declaration, or supporting-statement data.",
      "Run dryRun=true first for unfamiliar forms; inspect unmapped fields, AI mapping confidence, and the verification report before trusting final PDFs.",
      "Use useAiMapping=true when field names are unclear or the form layout differs from previously cached forms.",
    ],
    parameters: FillPdfFormParamsSchema,
    executionMode: "sequential",
    execute: async (_toolCallId, params: FillPdfFormParams, signal) => {
      const python = await ensurePython(pi, extDir, signal);
      const script = join(extDir, "fill_any_form.py");
      const pdf = normalizePath(params.pdf);
      const data = params.data ? normalizePath(params.data) : join(extDir, "general_data");
      const args = [
        script,
        pdf,
        "--data",
        data,
        "--sector",
        params.sector ?? "general",
        "--mappings-dir",
        join(extDir, "mappings"),
        "--logs-dir",
        join(extDir, "logs"),
      ];

      if (params.out) args.push("--out", normalizePath(params.out));
      if (params.forceRemap) args.push("--force-remap");
      if (params.dryRun) args.push("--dry-run");
      if (params.useAiMapping) args.push("--use-ollama-fallback");
      if (params.ollamaModel) args.push("--ollama-model", params.ollamaModel);
      if (typeof params.minConfidence === "number") args.push("--min-confidence", String(params.minConfidence));

      const result = await pi.exec(python, args, { cwd: extDir, signal });
      const text = [result.stdout.trim(), result.stderr.trim()].filter(Boolean).join("\n");

      return {
        content: [{ type: "text", text: text || "PDF form filler completed." }],
        details: {
          pdf,
          data,
          exitCode: result.code,
          stdout: result.stdout,
          stderr: result.stderr,
        },
        isError: result.code !== 0,
      };
    },
  });
}
