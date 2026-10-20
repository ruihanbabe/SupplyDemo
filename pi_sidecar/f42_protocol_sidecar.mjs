// F42 P1: Pi owns the in-memory turn/tool loop; Python remains the only tool host.
import { Agent } from "@mariozechner/pi-agent-core";
import { createAssistantMessageEventStream, streamSimple } from "@mariozechner/pi-ai";
import readline from "node:readline";
import { Type } from "typebox";

const ENGINE = "pi-agent-core@0.73.1";
const replayModel = {
  id: "f42-replay",
  name: "F42 deterministic replay",
  api: "openai-completions",
  provider: "replay",
  baseUrl: "http://replay.invalid",
  reasoning: false,
  input: ["text"],
  cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
  contextWindow: 1024,
  maxTokens: 128,
};
const gatewayUrl = process.env.SUPPLYAGENT_GATEWAY_URL;
const gatewayCapability = process.env.SUPPLYAGENT_GATEWAY_CAPABILITY;
const gatewayModel = gatewayUrl && gatewayCapability ? {
  ...replayModel,
  id: "gateway-policy",
  name: "SupplyAgent model gateway",
  provider: "supplyagent",
  baseUrl: gatewayUrl,
} : undefined;

const rl = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
const toolResults = new Map();
let activeAgent;
let cancelled = false;
let running = false;

function send(message) {
  process.stdout.write(`${JSON.stringify(message)}\n`);
}

function assistantMessage(content, stopReason) {
  return {
    role: "assistant",
    content,
    api: replayModel.api,
    provider: replayModel.provider,
    model: replayModel.id,
    usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0, cost: replayModel.cost },
    stopReason,
    timestamp: Date.now(),
  };
}

function findCitation(result) {
  if (!result || typeof result !== "object") return undefined;
  if (result.citation) return result.citation;
  if (Array.isArray(result.hits) && result.hits[0]?.citation) return result.hits[0].citation;
  return undefined;
}

function replayArguments(toolName) {
  if (toolName === "supply.impact") return { partNumber: "PART-A" };
  if (toolName === "knowledge.search") return { query: "approval", limit: 5 };
  return {};
}

function replayStream(start) {
  return function stream(_model, context, options) {
  const stream = createAssistantMessageEventStream();
  const replayToolResults = context.messages.filter((message) => message.role === "toolResult");
  const toolResultCount = replayToolResults.length;
  const latestCitation = findCitation(replayToolResults.at(-1)?.details);
  let requested = {};
  try { requested = JSON.parse(start.task); } catch { /* legacy supply replay */ }
  const role = requested.role ?? "supply_risk";
  const toolName = start.tools[0]?.name ?? "supply.impact";
  const requiredCalls = toolName === "supply.impact" ? 2 : 1;
  const replayOutput = {
    role,
    complete: true,
    claims: [
      {
        subject: requested.opportunityRef ?? "PART-A",
        claimType: `${role}_replay`,
        conclusion: `${role} completed from its authorized read-only evidence.`,
        quality: "supported",
        citations: [
          latestCitation ?? {
            sourceKind: "tool_result",
            sourceId: "PART-A",
            version: "replay-v1",
            locator: "tool:supply.impact",
            fragmentHash: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
          },
        ],
        unknowns: [],
        conflicts: [],
      },
    ],
  };
  const message = toolResultCount === requiredCalls
    ? assistantMessage([{ type: "text", text: JSON.stringify(replayOutput) }], "stop")
    : assistantMessage(
        [{
          type: "toolCall",
          id: `${toolName === "supply.impact" ? "impact" : "tool"}-${toolResultCount + 1}`,
          name: toolName,
          arguments: replayArguments(toolName),
        }],
        "toolUse",
      );
  const fail = () => {
    const error = { ...assistantMessage([], "aborted"), errorMessage: "cancelled" };
    stream.push({ type: "error", reason: "aborted", error });
  };
  if (options.signal.aborted) queueMicrotask(fail);
  else options.signal.addEventListener("abort", fail, { once: true });
  queueMicrotask(() => {
    if (options.signal.aborted) return;
    stream.push({ type: "start", partial: message });
    if (toolResultCount === requiredCalls) {
      stream.push({ type: "text_start", contentIndex: 0, partial: message });
      stream.push({ type: "text_delta", contentIndex: 0, delta: message.content[0].text, partial: message });
      stream.push({ type: "text_end", contentIndex: 0, content: message.content[0].text, partial: message });
    }
    stream.push({ type: "done", reason: message.stopReason, message });
  });
  return stream;
  };
}

async function runTurn(start) {
  let modelCall = 0;
  let finalText = "";
  const toolDefinitions = start.tools.map(({ name }) => ({
    name,
    label: name,
    description: "Execute the server-authorized read-only tool.",
    parameters: name === "supply.impact"
      ? Type.Object({ partNumber: Type.String(), asOf: Type.Optional(Type.String()) }, { additionalProperties: false })
      : name === "knowledge.search"
        ? Type.Object({ query: Type.String(), limit: Type.Optional(Type.Number()) }, { additionalProperties: false })
        : Type.Object({}, { additionalProperties: false }),
    async execute(callId, _params, signal) {
      const result = await toolResults.get(callId);
      if (cancelled || signal?.aborted) throw new Error("cancelled");
      return { content: [{ type: "text", text: JSON.stringify(result) }], details: result };
    },
  }));
  const agent = new Agent({
    initialState: {
      systemPrompt: "Use only the registered supply tool.",
      model: gatewayModel ?? replayModel,
      tools: toolDefinitions,
    },
    convertToLlm: (messages) => messages,
    streamFn: gatewayModel
      ? (model, context, options) => streamSimple(model, context, {
          ...options,
          apiKey: gatewayCapability,
          headers: { "x-supplyagent-call-key": `${start.turnId}-${++modelCall}` },
          maxRetries: 0,
        })
      : replayStream(start),
    toolExecution: "sequential",
  });
  activeAgent = agent;
  agent.subscribe((event) => {
    if (event.type === "agent_start") {
      send({ protocolVersion: 1, type: "turn.started", turnId: start.turnId, engine: ENGINE });
    }
    if (event.type === "tool_execution_start") {
      let resolve;
      toolResults.set(event.toolCallId, new Promise((done) => { resolve = done; }));
      toolResults.set(`${event.toolCallId}:resolve`, resolve);
      send({
        protocolVersion: 1,
        type: "tool.call",
        turnId: start.turnId,
        callId: event.toolCallId,
        name: event.toolName,
        arguments: event.args,
      });
    }
    if (event.type === "message_update" && event.assistantMessageEvent.type === "text_delta") {
      finalText += event.assistantMessageEvent.delta;
      send({ protocolVersion: 1, type: "model.delta", turnId: start.turnId, delta: event.assistantMessageEvent.delta });
    }
  });
  await agent.prompt(start.task);
  let output;
  try {
    const parsed = JSON.parse(finalText);
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) output = parsed;
  } catch {
    // A generic turn may finish with text. Python business callers decide whether output is required.
  }
  send(
    cancelled
      ? { protocolVersion: 1, type: "turn.cancelled", turnId: start.turnId, code: "cancelled" }
      : {
          protocolVersion: 1,
          type: "turn.completed",
          turnId: start.turnId,
          status: "completed",
          ...(output ? { output } : {}),
        },
  );
  activeAgent = undefined;
}

for await (const line of rl) {
  const message = JSON.parse(line);
  if (!running && message.protocolVersion === 1 && message.type === "turn.start") {
    running = true;
    runTurn(message).then(() => rl.close()).catch(() => {
      send({ protocolVersion: 1, type: "turn.failed", turnId: message.turnId, code: "pi_replay_failed" });
      process.exitCode = 1;
      rl.close();
    });
  } else if (message.protocolVersion === 1 && message.type === "tool.result") {
    const resolve = toolResults.get(`${message.callId}:resolve`);
    if (resolve) resolve(message.result);
  } else if (message.protocolVersion === 1 && message.type === "turn.cancel" && activeAgent) {
    cancelled = true;
    activeAgent.abort();
    send({ protocolVersion: 1, type: "turn.cancelled", turnId: message.turnId, code: "cancelled" });
    rl.close();
    process.exit(0);
  }
}
