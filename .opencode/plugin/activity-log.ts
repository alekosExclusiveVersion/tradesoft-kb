import { appendFileSync, existsSync, mkdirSync, readdirSync, unlinkSync } from "node:fs"
import path from "node:path"
import type { Plugin } from "@opencode-ai/plugin"

const MAX_ARG_CHARS = 600
const MAX_TEXT_CHARS = 2000
const MAX_LOG_DAYS = 30

const SECRET_KEYS =
  /(password|passwd|secret|token|api[_-]?key|authorization|private[_-]?key|^key$|access[_-]?key)/i
const SECRET_VALUES =
  /(Bearer\s+[A-Za-z0-9._~+/=-]{6,}|sk-[A-Za-z0-9]{8,}|ghp_[A-Za-z0-9]{20,}|opaque-[A-Za-z0-9]+)/i

function redact(value: unknown): string {
  let s = typeof value === "string" ? value : JSON.stringify(value)
  s = s.replace(SECRET_VALUES, "[REDACTED]")
  return s.length > MAX_ARG_CHARS ? `${s.slice(0, MAX_ARG_CHARS)}…` : s
}

function maskKey(key: string, value: unknown): unknown {
  if (SECRET_KEYS.test(key) && typeof value === "string") {
    return "[REDACTED]"
  }
  return value
}

function sanitizeArgs(tool: string, args: Record<string, unknown>): Record<string, unknown> {
  const out: Record<string, unknown> = {}
  for (const [key, value] of Object.entries(args ?? {})) {
    out[key] = maskKey(key, value)
  }

  switch (tool) {
    case "bash": {
      const cmd = String(out.command ?? "")
      return { command: redact(cmd), workdir: out.workdir }
    }
    case "edit":
    case "write": {
      const content = out.content ?? ""
      return { filePath: out.filePath, contentLength: typeof content === "string" ? content.length : JSON.stringify(content).length }
    }
    case "read":
      return { filePath: out.filePath, offset: out.offset, limit: out.limit }
    case "webfetch":
      return { url: redact(out.url), format: out.format }
    case "websearch":
      return { query: redact(out.query), numResults: out.numResults }
    case "task": {
      const prompt = out.prompt ?? ""
      return {
        description: out.description,
        subagent_type: out.subagent_type,
        promptLength: typeof prompt === "string" ? prompt.length : JSON.stringify(prompt).length,
      }
    }
    case "glob":
      return { pattern: out.pattern, path: out.path }
    case "grep":
      return { pattern: redact(out.pattern), include: out.include, path: out.path }
    default:
      return out
  }
}

function iso(ts: number): string {
  return new Date(ts).toISOString()
}

function dayStamp(ts: number): string {
  return iso(ts).slice(0, 10)
}

function cleanOldLogs(logDir: string): void {
  const cutoff = Date.now() - MAX_LOG_DAYS * 24 * 60 * 60 * 1000
  for (const file of readdirSync(logDir)) {
    if (!file.startsWith("opencode-activity-") || !file.endsWith(".jsonl")) continue
    const day = file.slice("opencode-activity-".length, -".jsonl".length)
    if (/^\d{4}-\d{2}-\d{2}$/.test(day)) {
      const fileDate = new Date(day + "T00:00:00Z").getTime()
      if (fileDate < cutoff) {
        try {
          unlinkSync(path.join(logDir, file))
        } catch {
          // не роняем opencode на ошибке удаления
        }
      }
    }
  }
}

export default (async ({ directory }) => {
  const logDir = path.join(directory, "logs")
  if (!existsSync(logDir)) mkdirSync(logDir, { recursive: true })
  try {
    cleanOldLogs(logDir)
  } catch {
    // ротация — best effort
  }

  let currentDay = ""
  let logFile = ""
  const assistantLogged = new Set<string>()

  function write(entry: Record<string, unknown>) {
    const day = dayStamp(Date.now())
    if (day !== currentDay) {
      currentDay = day
      logFile = path.join(logDir, `opencode-activity-${day}.jsonl`)
    }
    appendFileSync(logFile, JSON.stringify({ ts: iso(Date.now()), ...entry }) + "\n")
  }

  return {
    "chat.message": async (input, output) => {
      const textParts = (output.parts ?? [])
        .filter((p) => p.type === "text")
        .map((p: any) => p.text ?? "")
        .join("")
      write({
        type: "chat.message",
        sessionID: input.sessionID,
        agent: input.agent,
        model: input.model,
        text: redact(textParts.slice(0, 2000)),
      })
    },

    "tool.execute.before": async (input, output) => {
      write({
        type: "tool.execute.before",
        sessionID: input.sessionID,
        tool: input.tool,
        callID: input.callID,
        args: sanitizeArgs(input.tool, output.args ?? {}),
      })
    },

    "tool.execute.after": async (input, output) => {
      write({
        type: "tool.execute.after",
        sessionID: input.sessionID,
        tool: input.tool,
        callID: input.callID,
        title: redact(output.title ?? ""),
        outputLength: (output.output ?? "").length,
        outputPrefix: redact((output.output ?? "").slice(0, 500)),
      })
    },

    "command.execute.before": async (input) => {
      write({
        type: "command.execute.before",
        sessionID: input.sessionID,
        command: input.command,
        args: redact(input.arguments ?? "").slice(0, 500),
      })
    },

    event: async ({ event }) => {
      const props = (event as any).properties ?? {}
      switch (event.type) {
        case "session.created":
          write({ type: "session.created", sessionID: props.info?.id, title: redact(props.info?.title ?? "") })
          break
        case "session.idle":
          write({ type: "session.idle", sessionID: props.sessionID })
          break
        case "session.status":
          write({
            type: "session.status",
            sessionID: props.sessionID,
            status: props.status?.type,
            message: props.status?.message ? redact(props.status.message) : undefined,
          })
          break
        case "command.executed":
          write({
            type: "command.executed",
            sessionID: props.sessionID,
            command: props.name,
            args: redact(props.arguments ?? "").slice(0, 300),
          })
          break
        case "permission.updated":
          write({
            type: "permission.updated",
            sessionID: props.sessionID,
            pattern: Array.isArray(props.pattern) ? props.pattern.join(" | ") : props.pattern,
            title: redact(props.title ?? ""),
          })
          break
        case "message.updated": {
          const info = props.info
          if (info?.role === "user") {
            const parts: any[] = info.parts ?? []
            const text = parts.filter((p) => p.type === "text").map((p) => p.text ?? "").join("")
            write({
              type: "message.user",
              sessionID: info.sessionID,
              agent: info.agent,
              text: redact(text.slice(0, MAX_TEXT_CHARS)),
            })
          }
          break
        }
        case "message.part.updated": {
          const part = props.part as any
          if (part?.type === "text" && part.time?.end && part.text && !assistantLogged.has(part.id)) {
            assistantLogged.add(part.id)
            if (assistantLogged.size > 1000) assistantLogged.clear()
            write({
              type: "message.assistant",
              sessionID: part.sessionID,
              messageID: part.messageID,
              model: part.modelID ? `${part.providerID}/${part.modelID}` : undefined,
              text: redact(String(part.text).slice(0, MAX_TEXT_CHARS)),
            })
          }
          break
        }
        case "file.edited":
          write({ type: "file.edited", file: redact(props.file ?? "") })
          break
        default:
          break
      }
    },
  }
}) satisfies Plugin