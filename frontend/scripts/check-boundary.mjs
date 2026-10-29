import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative } from "node:path";

const root = join(import.meta.dirname, "..");
const sourceRoot = join(root, "src");
const forbidden = ["from \"pg\"", "from 'pg'", "postgresql://", "SUPABASE_SERVICE_ROLE_KEY"];
const removedLegacyRoots = ["src/lib/auth", "src/lib/crm", "src/lib/db", "src/lib/gateway", "src/lib/pi", "src/lib/runtime", "src/lib/supply", "src/lib/trace"];

function files(directory) {
  return readdirSync(directory).flatMap((name) => {
    const path = join(directory, name);
    return statSync(path).isDirectory() ? files(path) : [path];
  });
}

const violations = files(sourceRoot).flatMap((path) => {
  const text = readFileSync(path, "utf8");
  const sourcePath = relative(root, path);
  const denied = forbidden.filter((needle) => text.includes(needle)).map((needle) => `${sourcePath}: ${needle}`);
  const serverDatabaseAccess = text.includes('from "postgres"');
  const clientDatabaseAccess = text.includes('"use client"') && text.includes("SUPPLYAGENT_PRODUCT_DATABASE_URL");
  if (serverDatabaseAccess) denied.push(`${sourcePath}: Next frontend must not import a PostgreSQL client`);
  if (clientDatabaseAccess) denied.push(`${sourcePath}: browser module must not read product database URL`);
  return denied;
});

const removedLegacyFiles = files(sourceRoot)
  .map((path) => relative(root, path))
  .filter((sourcePath) => removedLegacyRoots.some((directory) => sourcePath.startsWith(`${directory}/`)));
violations.push(...removedLegacyFiles.map((sourcePath) => `${sourcePath}: removed legacy service layer must not return`));

if (violations.length > 0) {
  console.error("F31 信任边界检查失败：\\n" + violations.join("\\n"));
  process.exit(1);
}
console.log("产品边界检查通过：浏览器源码无数据库连接或 service role；Next API route 仅作 Python API BFF，不导入领域或数据库实现。");
