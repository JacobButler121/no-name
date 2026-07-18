import { existsSync, readFileSync } from "node:fs";
import { spawn } from "node:child_process";

const projectEnvironment = { ...process.env };
const environmentPath = ".env.local";

if (existsSync(environmentPath)) {
  for (const line of readFileSync(environmentPath, "utf8").split(/\r?\n/)) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#")) continue;
    const separator = trimmed.indexOf("=");
    if (separator < 1) continue;
    const key = trimmed.slice(0, separator).trim();
    let value = trimmed.slice(separator + 1).trim();
    if (
      (value.startsWith('"') && value.endsWith('"')) ||
      (value.startsWith("'") && value.endsWith("'"))
    ) {
      value = value.slice(1, -1);
    }
    projectEnvironment[key] ??= value;
  }
}

if (!existsSync(".venv/bin/python")) {
  console.error("Spotted's processor is not installed. Run: npm run setup:processor");
  process.exit(1);
}

if (!projectEnvironment.OPENAI_API_KEY) {
  console.warn(
    "OPENAI_API_KEY is not set. Video extraction will work, but live product detection will stop with a clear configuration error.",
  );
}

const children = [
  spawn(
    ".venv/bin/python",
    [
      "-m",
      "uvicorn",
      "processor.main:app",
      "--host",
      "127.0.0.1",
      "--port",
      "8000",
    ],
    { stdio: "inherit", env: projectEnvironment },
  ),
  spawn("npm", ["run", "dev"], {
    stdio: "inherit",
    env: projectEnvironment,
  }),
];

let stopping = false;
function stop(signal = "SIGTERM") {
  if (stopping) return;
  stopping = true;
  for (const child of children) {
    if (!child.killed) child.kill(signal);
  }
}

for (const signal of ["SIGINT", "SIGTERM"]) {
  process.on(signal, () => stop(signal));
}

for (const child of children) {
  child.on("exit", (code, signal) => {
    if (!stopping && code !== 0) {
      console.error(`A Spotted service stopped unexpectedly (${signal ?? code}).`);
      stop();
      process.exitCode = code ?? 1;
    }
  });
}
