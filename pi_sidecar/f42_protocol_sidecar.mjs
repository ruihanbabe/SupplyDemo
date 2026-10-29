// F42 P1: Pi owns the in-memory turn/tool loop; Python remains the only tool host.
import { Agent } from "@mariozechner/pi-agent-core";
import { createAssistantMessageEventStream, streamSimple } from "@mariozechner/pi-ai";
import readline from "node:readline";
import { ContextBridge } from "./context_protocol.mjs";

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


const rl = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
const toolResults = new Map();
let activeAgent;
let contextBridge, readyContext;
let cancelled = false;
let running = false;
let protocolVersion = 1, outgoingSeq = 0, incomingSeq = 0, turnId;
let batchNumber = 0, totalCalls = 0, activeBatch;
const outcomes = new Map();
const seenCalls = new Set();
const messageKeys = new WeakMap();
const MAX_BYTES = 16384;
const TOOL_PROTOCOL = "python-batch-v2";

function send(message) {
  const payload = protocolVersion >= 2 ? {...message, protocolVersion, seq: ++outgoingSeq} : message;
  const line = `${JSON.stringify(payload)}\n`;
  if (Buffer.byteLength(line) > MAX_BYTES) throw new Error("JSONL limit");
  process.stdout.write(line);
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
        conclusion: requested.mission === "opportunity_intelligence"
          ? `本地 Replay：已读取 ${role} 的授权证据。问题「${requested.question}」尚未经过真实模型判断。`
          : `${role} completed from its authorized read-only evidence.`,
        quality: requested.mission === "opportunity_intelligence" ? "needs_review" : "supported",
        citations: [
          latestCitation ?? {
            sourceKind: "tool_result",
            sourceId: "PART-A",
            version: "replay-v1",
            locator: "tool:supply.impact",
            fragmentHash: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
          },
        ],
        unknowns: requested.mission === "opportunity_intelligence" ? ["确定性 Replay 只验证运行链路，未验证业务回答质量。"] : [],
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
  let gatewayModel;
  let policyHash;
  if (gatewayUrl && gatewayCapability) {
    const response = await fetch(`${gatewayUrl.replace(/\/$/, "")}/model`, {headers: {authorization: `Bearer ${gatewayCapability}`}});
    if (!response.ok) throw new Error("model profile unavailable");
    const profile = await response.json();
    gatewayModel = {...profile.model, baseUrl: gatewayUrl};
    policyHash = profile.policyHash;
  }
  const internalNames = new Map(start.tools.map(t => [gatewayModel ? t.wireName : t.name, t.name]));
  const toolDefinitions = start.tools.map(({ name, wireName, description, inputSchema }) => ({
    name: gatewayModel ? wireName : name,
    label: name,
    description,
    parameters: inputSchema,
    async execute(callId, _params, signal) {
      let result = await toolResults.get(callId);
      if (protocolVersion >= 2) {
        if (!result || result.status !== "ok") throw new Error(result?.code ?? "tool_failed");
        result = result.modelPayload;
      }
      if (cancelled || signal?.aborted) throw new Error("cancelled");
      return { content: [{ type: "text", text: JSON.stringify(result) }], details: result };
    },
  }));
  function normalized(messages) {
    return messages.map(message => {
      if (message.role !== "toolResult" || !outcomes.has(message.toolCallId)) return message;
      const result = outcomes.get(message.toolCallId);
      return {...message, content: [{type: "text", text: JSON.stringify(result.modelPayload)}],
        details: result.modelPayload, isError: result.status !== "ok"};
    });
  }
  if (protocolVersion === 3) contextBridge = new ContextBridge(start, send);
  const agent = new Agent({
    initialState: {
      systemPrompt: start.instructions ?? "Use only the registered supply tool.",
      model: gatewayModel ?? (protocolVersion === 3 ? {...replayModel, contextWindow: 32768} : replayModel),
      tools: toolDefinitions,
    },
    convertToLlm: async (messages) => {
      if (protocolVersion < 2) return messages;
      if (activeBatch) {
        await activeBatch.done;
        const ids = new Set(activeBatch.calls.map(call => call.id));
        const results = messages.filter(message => message.role === "toolResult" && ids.has(message.toolCallId));
        if (results.length !== activeBatch.calls.length || results.some((result, i) => result.toolCallId !== activeBatch.calls[i].id)) throw new Error("missing/duplicate result projection");
      }
      // Pi local refusals are replaced by Python's accepted feedback first.
      const source = normalized(messages);
      if (protocolVersion !== 3) return source;
      if (readyContext) throw new Error("unconsumed context ready");
      readyContext = await contextBridge.transfer("input", source, `${start.turnId}-${++modelCall}`);
      if (gatewayModel && readyContext.ready.policyHash !== policyHash) throw new Error("context model policy changed");
      return readyContext.projection.messages;
    },
    streamFn: async (model, context, options) => {
      const prepared = readyContext;
      readyContext = undefined;
      if (protocolVersion === 3 && !prepared) throw new Error("context prepare required");
      const key = protocolVersion === 3 ? prepared.ready.modelCallKey : gatewayModel ? `${start.turnId}-${++modelCall}` : null;
      const projected = prepared ? {...context, messages: prepared.projection.messages, systemPrompt: prepared.projection.systemPrompt} : context;
      if (prepared && !gatewayModel) await contextBridge.consumeLocal(prepared.ready);
      const response = gatewayModel ? streamSimple(model, projected, {
        ...options, apiKey: gatewayCapability,
        headers: {"x-supplyagent-call-key": key, "x-supplyagent-policy-hash": policyHash,
          ...(prepared ? {"x-supplyagent-context-snapshot": prepared.ready.snapshotId,
            "x-supplyagent-projection-hash": prepared.ready.projectionHash} : {})}, maxRetries: 0,
      }) : replayStream(start)(model, projected, options);
      return {
        [Symbol.asyncIterator]: () => response[Symbol.asyncIterator](),
        async result() {
          const message = await response.result();
          messageKeys.set(message, gatewayModel ? key : null);
          return message;
        },
      };
    },
    toolExecution: "sequential",
  });
  activeAgent = agent;
  agent.subscribe((event) => {
    if (event.type === "agent_start") {
      send({ protocolVersion: 1, type: "turn.started", turnId: start.turnId, engine: ENGINE, ...(protocolVersion >= 2 ? {toolProtocol: TOOL_PROTOCOL} : {}) });
    }
    if (protocolVersion >= 2 && event.type === "message_end" && event.message.role === "assistant" && !["error", "aborted"].includes(event.message.stopReason)) {
      const calls = event.message.content.filter(b => b.type === "toolCall");
      if (calls.length) {
        if (activeBatch && !activeBatch.complete) throw new Error("overlapping batch");
        if (calls.length > 16 || (totalCalls += calls.length) > 16) throw new Error("tool budget");
        // Install every slot synchronously before notifying Python or executing any Pi tool.
        for (const call of calls) {
          if (!/^[a-zA-Z0-9_|-]{1,200}$/.test(call.id) || seenCalls.has(call.id)) throw new Error("duplicate or invalid call");
          seenCalls.add(call.id);
          let resolve;
          toolResults.set(call.id, new Promise(done => {resolve = done;}));
          toolResults.set(`${call.id}:resolve`, resolve);
        }
        let resolve;
        const done = new Promise(r => {resolve = r;});
        activeBatch = {id: ++batchNumber, calls, done, resolve, complete: false};
        send({type: "tool.batch", turnId: start.turnId, batchId: activeBatch.id,
          modelCallKey: messageKeys.get(event.message) ?? null,
          calls: calls.map((call, index) => ({index, callId: call.id,
            name: internalNames.get(call.name) ?? call.name, arguments: call.arguments}))});
      }
    }
    if (protocolVersion === 1 && event.type === "tool_execution_start") {
      let resolve;
      toolResults.set(event.toolCallId, new Promise((done) => { resolve = done; }));
      toolResults.set(`${event.toolCallId}:resolve`, resolve);
      send({
        protocolVersion: 1,
        type: "tool.call",
        turnId: start.turnId,
        callId: event.toolCallId,
        name: internalNames.get(event.toolName),
        ...(gatewayModel ? {modelCallKey: `${start.turnId}-${modelCall}`} : {}),
        arguments: event.args,
      });
    }
    if (event.type === "message_update" && event.assistantMessageEvent.type === "text_delta") {
      send({ protocolVersion: 1, type: "model.delta", turnId: start.turnId, delta: event.assistantMessageEvent.delta });
    }
  });
  await agent.prompt(start.task);
  if (protocolVersion >= 2 && activeBatch && !activeBatch.complete) throw new Error("unsettled final batch");
  const finalMessage = agent.state.messages.filter(m => m.role === "assistant").at(-1);
  if (!finalMessage || finalMessage.stopReason !== "stop") throw new Error("model did not complete");
  const finalText = finalMessage.content.filter(b => b.type === "text").map(b => b.text).join("");
  let output;
  try {
    const parsed = JSON.parse(finalText);
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) output = parsed;
  } catch {
    // A generic turn may finish with text. Python business callers decide whether output is required.
  }
  if (protocolVersion === 3 && !cancelled) await contextBridge.transfer("final", normalized(agent.state.messages), `${start.turnId}-${modelCall}`);
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
  if (Buffer.byteLength(line) + 1 > MAX_BYTES) throw new Error("JSONL limit");
  const message = JSON.parse(line);
  if (!running) {
    protocolVersion = message.protocolVersion;
    turnId = message.turnId;
    if (![1,2,3].includes(protocolVersion)) throw new Error("protocol version");
  }
  if (message.protocolVersion !== protocolVersion || message.turnId !== turnId) throw new Error("turn mismatch");
  if (protocolVersion >= 2 && (message.seq !== ++incomingSeq || !Number.isInteger(message.seq))) throw new Error("sequence mismatch");
  if (!running && message.type === "turn.start") {
    if (protocolVersion >= 2) {
      const expected = ["protocolVersion","seq","type","turnId","task","context","tools","toolProtocol","bindingVersion","bindingHash", ...(message.instructions === undefined ? [] : ["instructions"]), ...(protocolVersion === 3 ? ["contextProtocol", "contextLimits"] : [])];
      if (Object.keys(message).sort().join() !== expected.sort().join()) throw new Error("start fields");
    }
    if (protocolVersion >= 2 && (message.toolProtocol !== TOOL_PROTOCOL || message.bindingVersion !== 1 || !/^[a-f0-9]{64}$/.test(message.bindingHash))) throw new Error("binding/protocol mismatch");
    running = true;
    runTurn(message).then(() => rl.close()).catch(() => {
      send({ protocolVersion: 1, type: "turn.failed", turnId: message.turnId, code: "model_failed" });
      process.exitCode = 1;
      rl.close();
    });
  } else if (protocolVersion === 3 && message.type.startsWith("context.")) {
    contextBridge.receive(message);
  } else if (protocolVersion >= 2 && message.type === "tool.batch.result") {
    const batch = activeBatch;
    if (Object.keys(message).sort().join() !== ["protocolVersion","seq","type","turnId","batchId","results"].sort().join() || !batch || batch.complete || message.batchId !== batch.id || !Array.isArray(message.results) || message.results.length !== batch.calls.length) throw new Error("batch result mismatch");
    message.results.forEach((result, index) => {
      if (Object.keys(result).sort().join() !== ["index","callId","status","code","modelPayload","resultRef"].sort().join() || result.index !== index || result.callId !== batch.calls[index].id || !["ok","error"].includes(result.status) || (result.status === "ok" ? result.code !== null : typeof result.code !== "string")) throw new Error("result slot mismatch");
    });
    for (const result of message.results) {
      outcomes.set(result.callId, result);
      toolResults.get(`${result.callId}:resolve`)(result);
    }
    batch.complete = true;
    batch.resolve();
  } else if (protocolVersion === 1 && message.type === "tool.result") {
    const resolve = toolResults.get(`${message.callId}:resolve`);
    if (resolve) resolve(message.result);
  } else if (message.type === "turn.cancel" && activeAgent) {
    cancelled = true;
    activeAgent.abort();
    send({ protocolVersion: 1, type: "turn.cancelled", turnId: message.turnId, code: "cancelled" });
    rl.close();
    process.exit(0);
  } else {
    throw new Error("unexpected message");
  }
}
