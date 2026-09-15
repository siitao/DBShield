/**
 * 解析 SHOW GRANTS FOR 的原始授权语句为结构化权限，用于账号授权弹窗回显。
 *
 * 语句形态（MySQL / MariaDB）：
 *   GRANT SELECT, INSERT ON *.* TO `u`@`h`                → 全局
 *   GRANT SELECT, UPDATE ON `db`.* TO `u`@`h`             → 库级
 *   GRANT SELECT ON `db`.`tb` TO `u`@`h`                  → 表级
 *   GRANT SELECT (c1, c2) ON `db`.`tb` TO `u`@`h`         → 列级
 * 另有 GRANT USAGE（无实际权限）、GRANT PROXY、ALL PRIVILEGES、
 * 8.0 动态全局权限（如 BINLOG_ADMIN）等形态。
 */

export interface ParsedGrants {
  /** 全局权限名（大写，ALL PRIVILEGES 不展开） */
  global: string[];
  /** 库名 → 权限名列表 */
  dbs: Record<string, string[]>;
  /** "db.tb" → 权限名列表 */
  tables: Record<string, string[]>;
  /** 列级权限的可读串，如 "SELECT(db.tb: c1,c2)" */
  columns: string[];
}

/** 按顶层逗号拆分（忽略括号内逗号），处理 "SELECT (a, b), UPDATE (c)" 形态 */
function splitTopLevel(text: string): string[] {
  const parts: string[] = [];
  let depth = 0;
  let cur = "";
  for (const ch of text) {
    if (ch === "(") depth++;
    else if (ch === ")") depth--;
    if (ch === "," && depth === 0) {
      parts.push(cur);
      cur = "";
    } else {
      cur += ch;
    }
  }
  if (cur.trim()) parts.push(cur);
  return parts.map((p) => p.trim()).filter(Boolean);
}

function unquoteIdent(name: string): string {
  return name.replace(/^`/, "").replace(/`$/, "").replace(/``/g, "`");
}

function pushUnique(list: string[], name: string): void {
  if (!list.includes(name)) list.push(name);
}

export function parseShowGrants(stmts: unknown): ParsedGrants {
  const result: ParsedGrants = { global: [], dbs: {}, tables: {}, columns: [] };
  if (!Array.isArray(stmts)) return result;
  for (const item of stmts) {
    const stmt = String(item ?? "")
      .trim()
      .replace(/;+\s*$/, "");
    // 只解析 GRANT <privs> ON <scope> TO ...；WITH GRANT OPTION 在 TO 之后，不影响
    const m = /^GRANT\s+(.+?)\s+ON\s+(.+?)\s+TO\s+/i.exec(stmt);
    if (!m) continue;
    const scope = m[2].trim();
    let level: "global" | "db" | "table" = "global";
    let dbName = "";
    let tbName = "";
    if (scope !== "*.*") {
      const sm = /^`((?:[^`]|``)+)`\.(?:\*|`((?:[^`]|``)+)`)$/.exec(scope);
      if (!sm) continue; // 无法识别的作用域（如部分代理授权）跳过
      dbName = unquoteIdent(sm[1]);
      level = sm[2] === undefined ? "db" : "table";
      if (level === "table") tbName = unquoteIdent(sm[2]);
    }
    for (const priv of splitTopLevel(m[1])) {
      // 列级形态：SELECT (c1, c2)
      const cm = /^(\S+)\s*\((.+)\)$/.exec(priv);
      if (cm && level === "table") {
        const line = `${cm[1].toUpperCase()}(${dbName}.${tbName}: ${cm[2].trim()})`;
        if (!result.columns.includes(line)) result.columns.push(line);
        continue;
      }
      const name = priv.toUpperCase();
      if (name === "USAGE" || name === "PROXY") continue; // 无实际权限/代理授权
      if (level === "global") {
        pushUnique(result.global, name);
      } else if (level === "db") {
        result.dbs[dbName] = result.dbs[dbName] || [];
        pushUnique(result.dbs[dbName], name);
      } else {
        const key = `${dbName}.${tbName}`;
        result.tables[key] = result.tables[key] || [];
        pushUnique(result.tables[key], name);
      }
    }
  }
  return result;
}
