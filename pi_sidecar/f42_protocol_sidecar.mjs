// F42 fixture sidecar: a real Node subprocess that speaks only protocol v1.
// It is deliberately not Pi upstream, a model client, or a tool host.
import readline from "node:readline";

const rl = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
let started = false;

function send(message) {
  process.stdout.write(`${JSON.stringify(message)}\n`);
}

rl.on("line", (line) => {
  const message = JSON.parse(line);
  if (!started) {
    if (message.protocolVersion !== 1 || message.type !== "turn.start") process.exitCode = 2;
    else {
      started = true;
      send({ protocolVersion: 1, type: "turn.started", turnId: message.turnId });
      send({ protocolVersion: 1, type: "tool.call", turnId: message.turnId, callId: "impact-1", name: "supply.impact", arguments: { partNumber: "PART-A" } });
    }
    return;
  }
  if (message.protocolVersion !== 1 || message.type !== "tool.result" || message.callId !== "impact-1") process.exitCode = 3;
  else send({ protocolVersion: 1, type: "turn.completed", turnId: message.turnId, status: "completed" });
  rl.close();
});
