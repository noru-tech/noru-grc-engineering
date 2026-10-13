#!/usr/bin/env node
/** Offline compiler-backed observations. Repository code/configuration is never executed. */
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { createRequire } from 'node:module';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { isMigrationPath, migrationPaths } from './collect.mjs';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const VENDOR = path.join(HERE, 'vendor/typescript');
const ADAPTER = { name: 'typescript-compiler', version: 1, compiler_version: '6.0.2' };
const require = createRequire(import.meta.url);
const hash = value => crypto.createHash('sha256').update(value).digest('hex');
let compiler, verifiedVendorFiles;
function loadCompiler() {
  if (compiler) return compiler;
  const manifest = JSON.parse(fs.readFileSync(path.join(VENDOR, 'INTEGRITY.json'), 'utf8'));
  if (manifest.version !== ADAPTER.compiler_version) throw new Error('Unexpected bundled compiler version');
  for (const name of ['lib/typescript.js','lib/lib.esnext.d.ts','LICENSE.txt','package.json']) if (typeof manifest.files?.[name] !== 'string') throw new Error('Bundled compiler integrity entry missing: '+name);
  for (const [name, digest] of Object.entries(manifest.files)) {
    if (name.includes('..') || path.isAbsolute(name) || hash(fs.readFileSync(path.join(VENDOR, name))) !== digest) {
      throw new Error('Bundled compiler integrity mismatch: ' + name);
    }
  }
  verifiedVendorFiles = new Set(Object.keys(manifest.files).map(name=>path.join(VENDOR,name)));
  compiler = require(path.join(VENDOR, 'lib/typescript.js'));
  if (compiler.version !== ADAPTER.compiler_version) throw new Error('Bundled compiler version mismatch');
  return compiler;
}
const relative = (root, name) => path.relative(root, name).split(path.sep).join('/');
const sourceExtension = /\.[cm]?[jt]sx?$/i;
const hardExcluded = name => isMigrationPath(name) || name.split('/').some(part => /^(?:node_modules|vendor|\.git|\.noru|\.next|dist|build|coverage)$/.test(part));
const excluded = name => name.split('/').some(part => /^(?:node_modules|vendor|\.git|\.noru|\.next|dist|build|coverage|__tests__|__fixtures__)$/.test(part)) || /(?:\.(?:test|spec)|[-_.]fixture)\.[cm]?[jt]sx?$/i.test(name);
function contained(root, file) {
  try { const real = fs.realpathSync(file); return real === root || real.startsWith(root + path.sep); } catch { return false; }
}
function discover(root) {
  try { return [...new Set(execFileSync('git', ['-C', root, 'ls-files', '-z', '--cached', '--others', '--exclude-standard'], { encoding: 'utf8', maxBuffer: 32 * 1024 * 1024, stdio: ['ignore','pipe','ignore'] }).split('\0').filter(Boolean))].sort(); }
  catch {
    const result = [];
    const walk = dir => {
      for (const entry of fs.readdirSync(dir, { withFileTypes: true }).sort((a,b) => a.name.localeCompare(b.name))) {
        const file = path.join(dir, entry.name), name = relative(root, file);
        if (excluded(name) || entry.isSymbolicLink()) continue;
        if (entry.isDirectory()) walk(file); else if (entry.isFile()) result.push(name);
        if (result.length > 100000) throw new Error('Source discovery exceeds the supported file bound');
      }
    };
    walk(root); return result;
  }
}

export function extractRepository(repository, options = {}) {
  const ts = loadCompiler(), root = fs.realpathSync(repository);
  const files = [...new Set([...(options.files ?? discover(root)), ...(options.includeTests ?? [])])].sort(), texts = new Map(), sources = [], gaps = [], facts = [], tables = [];
  const maxFiles = options.maxFiles ?? 20000, maxDepth = options.maxDepth ?? 8, maxMembers = options.maxMembers ?? 256;
  function gap(kind, subject, message, evidence = []) { gaps.push({ kind, subject, message, evidence }); }
  const explicitlyIncluded = new Set(options.includeTests ?? []);
  const extraExcluded = new Set(options.excludedPaths ?? []);
  const migrationRoots = [];
  const recognizedMigrations = new Set(migrationPaths(root, files));
  const supportSources = new Set(files.filter(name => isMigrationPath(name) && !recognizedMigrations.has(name)));
  for (const name of files.filter(name => /(?:^|\/)drizzle\.config\.[cm]?[jt]s$/.test(name) && !hardExcluded(name))) {
    const file=path.join(root,name); if(!contained(root,file)) continue;
    const text=fs.readFileSync(file,'utf8'), sf=ts.createSourceFile(file,text,ts.ScriptTarget.Latest,true), variables=new Map();
    const scan=node=>{if(ts.isVariableDeclaration(node)&&ts.isIdentifier(node.name)&&node.initializer) variables.set(node.name.text,node.initializer);ts.forEachChild(node,scan);};scan(sf);
    const unwrap=(node,seen=new Set())=>{
      if(!node || seen.has(node)) return undefined;seen.add(node);
      if(ts.isIdentifier(node)) return unwrap(variables.get(node.text),seen);
      if(ts.isParenthesizedExpression(node)||ts.isAsExpression(node)||ts.isSatisfiesExpression(node)) return unwrap(node.expression,seen);
      if(ts.isCallExpression(node)) return unwrap(node.arguments[0],seen);
      return node;
    };
    const exported=sf.statements.find(ts.isExportAssignment), object=unwrap(exported?.expression);
    const out=object&&ts.isObjectLiteralExpression(object)?object.properties.find(property=>ts.isPropertyAssignment(property)&&((ts.isIdentifier(property.name)||ts.isStringLiteralLike(property.name))&&property.name.text==='out')):undefined;
    const value=out&&ts.isPropertyAssignment(out)?unwrap(out.initializer):undefined;
    if(value&&ts.isStringLiteralLike(value)) {
      const target=relative(root,path.resolve(path.dirname(file),value.text));
      if(target && !target.startsWith('../') && !path.isAbsolute(target)) migrationRoots.push(target.replace(/\/$/,''));
      else gap('migration_configuration',name,'Migration output boundary is not an unambiguous repository subdirectory');
    } else if(out) gap('migration_configuration',name,'Migration output is dynamic and cannot be established without executing configuration');
  }
  const excludedSource = name => extraExcluded.has(name) || migrationRoots.some(directory=>name===directory||name.startsWith(directory+'/')) || recognizedMigrations.has(name) || (hardExcluded(name) && !supportSources.has(name)) || (excluded(name) && !explicitlyIncluded.has(name));
  const candidates = files.filter(name => sourceExtension.test(name) && !excludedSource(name));
  if (candidates.length > maxFiles) throw new Error('Source count exceeds the supported file bound');
  for (const name of candidates) {
    const file = path.join(root, name);
    if (!contained(root, file) || !fs.statSync(file).isFile()) { gap('source_boundary', name, 'Source is missing or resolves outside the repository'); continue; }
    const bytes = fs.readFileSync(file);
    if (bytes.length > (options.maxFileBytes ?? 4 * 1024 * 1024)) { gap('source_size', name, 'Source exceeds the supported size bound'); continue; }
    texts.set(file, bytes.toString('utf8')); sources.push({ path: name, sha256: hash(bytes) });
  }
  const sourceDigest = new Map(sources.map(s => [s.path, s.sha256]));
  const configFiles = files.filter(name => /(?:^|\/)tsconfig(?:\.[^/]*)?\.json$/.test(name) && !excluded(name));
  const configs = new Map(), packages = [];
  for (const name of files.filter(name => /(?:^|\/)package\.json$/.test(name) && !excluded(name))) {
    const file = path.join(root, name);
    if (!contained(root, file)) continue;
    try {
      const text = fs.readFileSync(file, 'utf8'), data = JSON.parse(text);
      if (typeof data.name === 'string') packages.push({ name: data.name, directory: path.dirname(file), data });
      sources.push({ path: name, sha256: hash(text) });
    } catch { gap('package_metadata', name, 'Package metadata is not valid static JSON'); }
  }
  function packageConfig(name) {
    for(const pkg of packages) if(name===pkg.name || name.startsWith(pkg.name+'/')) {
      const sub=name===pkg.name?'.':'./'+name.slice(pkg.name.length+1);
      let target=staticExport(pkg.data.exports?.[sub]);
      if(!target && sub==='.') target=typeof pkg.data.tsconfig==='string'?pkg.data.tsconfig:'tsconfig.json';
      if(!target && sub!=='.') target=sub;
      if(typeof target!=='string') continue;
      const base=path.resolve(pkg.directory,target);
      for(const file of [base,base+'.json',path.join(base,'tsconfig.json')]) if(file.endsWith('.json') && contained(root,file) && fs.statSync(file).isFile()) return file;
    }
    return undefined;
  }
  function readConfig(file, seen = new Set()) {
    if (seen.has(file) || seen.size>=20) {gap('configuration_cycle',relative(root,file),'Config inheritance is cyclic or exceeds the supported depth');return {};}
    if (!contained(root, file)) return {};
    seen.add(file);
    try {
      const text = fs.readFileSync(file, 'utf8');
      const parsed = ts.parseConfigFileTextToJson(file, text);
      sources.push({ path: relative(root, file), sha256: hash(text) });
      if (parsed.error) { gap('configuration_syntax', relative(root, file), 'Cannot parse TypeScript configuration as data'); return {}; }
      const data = parsed.config ?? {};
      let inherited = {};
      for(const extension of Array.isArray(data.extends)?data.extends:data.extends?[data.extends]:[]) {
        if(typeof extension!=='string') {gap('configuration_extends',relative(root,file),'Config inheritance must be a static string');continue;}
        let parent;
        if(extension.startsWith('.')) {parent=path.resolve(path.dirname(file),extension);if(!path.extname(parent)) parent+='.json';}
        else parent=packageConfig(extension);
        if(parent) inherited={...inherited,...readConfig(parent,new Set(seen))};
        else gap('configuration_extends',relative(root,file),'Config package is not available as an in-repository static JSON workspace file');
      }
      const local = data.compilerOptions ?? {};
      return { ...inherited, ...local,
        baseUrl: typeof local.baseUrl === 'string' ? path.resolve(path.dirname(file), local.baseUrl) : inherited.baseUrl ?? path.dirname(file),
        paths: local.paths ?? inherited.paths ?? {} };
    } catch { gap('configuration_read', relative(root, file), 'Configuration could not be read'); return {}; }
  }
  for (const name of configFiles) {const directory=path.dirname(path.join(root,name));if(!configs.has(directory)||path.basename(name)==='tsconfig.json') configs.set(directory,readConfig(path.join(root,name)));}
  function candidateFile(base) {
    const stem = /\.[cm]?jsx?$/.test(base) ? base.replace(/\.[cm]?jsx?$/, '') : base;
    for (const file of [base, stem + '.ts', stem + '.tsx', stem + '.mts', stem + '.cts', stem + '.d.ts', stem + '.js', stem + '.jsx', path.join(base, 'index.ts'), path.join(base, 'index.tsx'), path.join(base, 'index.d.ts')]) if (texts.has(path.normalize(file))) return path.normalize(file);
    return undefined;
  }
  function staticExport(value) {
    if (typeof value === 'string') return value;
    if (value && typeof value === 'object' && !Array.isArray(value)) return staticExport(value.types ?? value.import ?? value.default ?? value.require);
    return undefined;
  }
  function resolveModule(name, containingFile) {
    if (name.startsWith('.')) return candidateFile(path.resolve(path.dirname(containingFile), name));
    const config = [...configs.entries()].filter(([directory]) => containingFile.startsWith(directory + path.sep)).sort((a,b) => b[0].length - a[0].length)[0]?.[1];
    if (config) for (const [pattern, substitutions] of Object.entries(config.paths)) {
      if (pattern.split('*').length > 2) {
        gap('configuration_paths', pattern, 'TypeScript path patterns must contain at most one wildcard');
        continue;
      }
      const star = pattern.indexOf('*');
      const matched = star < 0 ? name === pattern : name.startsWith(pattern.slice(0, star)) && name.endsWith(pattern.slice(star + 1));
      if (!matched || !Array.isArray(substitutions)) continue;
      const middle = star < 0 ? '' : name.slice(star, name.length - (pattern.length - star - 1));
      for (const replacement of substitutions) if (typeof replacement === 'string') {
        if (replacement.split('*').length > 2) {
          gap('configuration_paths', pattern, 'TypeScript path substitutions must contain at most one wildcard');
          continue;
        }
        const found = candidateFile(path.resolve(config.baseUrl, replacement.replaceAll('*', () => middle)));
        if (found) return found;
      }
    }
    for (const pkg of packages) if (name === pkg.name || name.startsWith(pkg.name + '/')) {
      const sub = name === pkg.name ? '.' : './' + name.slice(pkg.name.length + 1);
      let target = staticExport(pkg.data.exports?.[sub]);
      if (sub === '.') target ??= staticExport(pkg.data.exports) ?? pkg.data.types ?? pkg.data.typings ?? pkg.data.main;
      if (!target && pkg.data.exports && typeof pkg.data.exports === 'object') for (const [pattern, value] of Object.entries(pkg.data.exports)) {
        if (!pattern.includes('*')) continue;
        const [before, after] = pattern.split('*');
        if (sub.startsWith(before) && sub.endsWith(after)) target = staticExport(value)?.replaceAll('*', () => sub.slice(before.length, sub.length - after.length));
      }
      if (typeof target === 'string') { const found = candidateFile(path.resolve(pkg.directory, target)); if (found) return found; }
      if (sub !== '.') { const found = candidateFile(path.resolve(pkg.directory, sub)); if (found) return found; }
    }
    return undefined;
  }
  const settings = { noEmit: true, allowJs: true, checkJs: false, skipLibCheck: true, target: ts.ScriptTarget.Latest, module: ts.ModuleKind.ESNext, strictNullChecks: true, noResolve: false, types: [] };
  const libDirectory = path.join(VENDOR, 'lib');
  const allowedRead = file => texts.has(path.normalize(file)) ? texts.get(path.normalize(file)) : verifiedVendorFiles.has(file) && file.startsWith(libDirectory + path.sep) && /^lib\.[\w.]+\.d\.ts$/.test(path.basename(file)) ? fs.readFileSync(file, 'utf8') : undefined;
  const host = {
    getSourceFile(file, languageVersion) { const text = allowedRead(file); return text === undefined ? undefined : ts.createSourceFile(file, text, languageVersion, true); },
    getDefaultLibFileName: () => path.join(libDirectory, 'lib.esnext.d.ts'), writeFile: () => { throw new Error('Compiler writes are disabled'); },
    getCurrentDirectory: () => root, getDirectories: () => [], fileExists: file => texts.has(path.normalize(file)) || verifiedVendorFiles.has(file),
    readFile: allowedRead, getCanonicalFileName: file => file, useCaseSensitiveFileNames: () => true, getNewLine: () => '\n',
    resolveModuleNames: (names, containingFile) => names.map(name => { const resolvedFileName = resolveModule(name, containingFile); return resolvedFileName ? { resolvedFileName, isExternalLibraryImport: false } : undefined; }),
  };
  const program = ts.createProgram([...texts.keys()], settings, host), checker = program.getTypeChecker();
  const syntaxSources = program.getSourceFiles().filter(sf => texts.has(sf.fileName));
  function span(node) {
    const sf = node.getSourceFile();
    if (!sf || !texts.has(sf.fileName)) return undefined;
    const start = node.getStart(sf), end = node.getEnd(), first = sf.getLineAndCharacterOfPosition(start), last = sf.getLineAndCharacterOfPosition(end);
    return { path: relative(root, sf.fileName), start_line: first.line + 1, end_line: last.line + 1, start_column: first.character + 1, end_column: last.character + 1, start, end, sha256: sourceDigest.get(relative(root, sf.fileName)), extractor: 'typescript-compiler@1' };
  }
  const evidence = node => { const item = node && span(node); return item ? [item] : []; };
  const nodeText = node => node?.getText(node.getSourceFile()) ?? '';
  const keyName = node => ts.isIdentifier(node) || ts.isStringLiteralLike(node) || ts.isNumericLiteral(node) ? node.text : undefined;
  const symbolAt = node => { let symbol = ts.isShorthandPropertyAssignment(node.parent) && node.parent.name === node ? checker.getShorthandAssignmentValueSymbol(node.parent) : checker.getSymbolAtLocation(node); if (symbol?.flags & ts.SymbolFlags.Alias) { try { symbol = checker.getAliasedSymbol(symbol); } catch { return undefined; } } return symbol; };
  const symbolId = node => { const symbol = symbolAt(node), declaration = symbol?.valueDeclaration ?? symbol?.declarations?.[0]; if (!declaration || !texts.has(declaration.getSourceFile().fileName)) return undefined; const scope=[]; for(let cursor=declaration.parent;cursor && !ts.isSourceFile(cursor);cursor=cursor.parent) if ((ts.isFunctionDeclaration(cursor)||ts.isMethodDeclaration(cursor)||ts.isClassDeclaration(cursor)) && cursor.name) scope.unshift(nodeText(cursor.name)); return relative(root,declaration.getSourceFile().fileName)+'#'+[...scope,symbol.name].join('.'); };
  function fact(kind, subject, object, node, detail = undefined) {
    const row = { kind, subject, ...(object === undefined ? {} : { object }), ...(detail === undefined ? {} : { detail }), evidence: evidence(node) };
    row.id = hash(JSON.stringify(row)).slice(0, 24); facts.push(row); return row;
  }
  function imported(node) {
    const local = checker.getSymbolAtLocation(node);
    for (const decl of local?.declarations ?? []) {
      let cursor = decl;
      while (cursor && !ts.isImportDeclaration(cursor)) cursor = cursor.parent;
      if (cursor && ts.isStringLiteralLike(cursor.moduleSpecifier)) return { module: cursor.moduleSpecifier.text, name: ts.isImportSpecifier(decl) ? (decl.propertyName ?? decl.name).text : nodeText(node), evidence: evidence(decl) };
    }
    return undefined;
  }
  function resolvedOrigin(origin, containingFile, seen=new Set()) {
    if(!origin || origin.module.startsWith('drizzle-orm/')) return origin;
    const file=resolveModule(origin.module,containingFile),key=file+'#'+origin.name;
    if(!file || seen.has(key) || seen.size>=12) return origin;
    seen.add(key);
    const sf=program.getSourceFile(file);if(!sf)return origin;
    for(const statement of sf.statements) {
      if(ts.isExportDeclaration(statement)) {
        const remote=statement.moduleSpecifier&&ts.isStringLiteralLike(statement.moduleSpecifier)?statement.moduleSpecifier.text:undefined;
        const specifier=statement.exportClause&&ts.isNamedExports(statement.exportClause)?statement.exportClause.elements.find(item=>item.name.text===origin.name):undefined;
        if(specifier) {
          const original=specifier.propertyName??specifier.name;
          const found=remote?resolvedOrigin({...origin,module:remote,name:original.text},file,new Set(seen)):callOrigin(original,new Set(seen));
          if(found?.module.startsWith('drizzle-orm/'))return found;
        } else if(remote&&!statement.exportClause) {
          const found=resolvedOrigin({...origin,module:remote},file,new Set(seen));
          if(found?.module.startsWith('drizzle-orm/'))return found;
        }
      }
      if(ts.isVariableStatement(statement)&&statement.modifiers?.some(modifier=>modifier.kind===ts.SyntaxKind.ExportKeyword)) {
        const declaration=statement.declarationList.declarations.find(item=>ts.isIdentifier(item.name)&&item.name.text===origin.name);
        if(declaration?.initializer){const found=callOrigin(declaration.initializer,new Set(seen));if(found?.module.startsWith('drizzle-orm/'))return found;}
      }
    }
    return origin;
  }
  function callOrigin(node,seen=new Set()) {
    if(!node)return undefined;
    let origin;
    if(ts.isIdentifier(node)) origin=imported(node);
    else if(ts.isPropertyAccessExpression(node)){const base=imported(node.expression);if(base)origin={...base,name:node.name.text};}
    if(origin)return resolvedOrigin(origin,node.getSourceFile().fileName,seen);
    if(ts.isIdentifier(node)) {
      const declaration=symbolAt(node)?.valueDeclaration;
      if(declaration&&ts.isVariableDeclaration(declaration)&&declaration.initializer&&!seen.has(declaration)){seen.add(declaration);return callOrigin(declaration.initializer,seen);}
    }
    return undefined;
  }
  function walk(node, visit) { visit(node); ts.forEachChild(node, child => walk(child, visit)); }
  function isSchemaWrapper(expression) {
    let declaration=symbolAt(expression)?.valueDeclaration;
    if(declaration&&ts.isVariableDeclaration(declaration))declaration=declaration.initializer;
    if(!declaration || !(ts.isFunctionDeclaration(declaration)||ts.isFunctionExpression(declaration)||ts.isArrowFunction(declaration)||ts.isMethodDeclaration(declaration)) || !declaration.body)return false;
    let found=false;
    walk(declaration.body,node=>{if(ts.isCallExpression(node)){const origin=callOrigin(node.expression);if(origin?.module.startsWith('drizzle-orm/')&&['pgTable','mysqlTable','sqliteTable'].includes(origin.name))found=true;}});
    return found;
  }
  function chain(node) {
    const calls = [];
    for (let cursor = node; ts.isCallExpression(cursor); cursor = ts.isPropertyAccessExpression(cursor.expression) ? cursor.expression.expression : undefined) {
      calls.push(cursor); if (!ts.isPropertyAccessExpression(cursor.expression)) break;
    }
    return calls;
  }
  function mergeMembers(branches) {
    const groups = new Map();
    branches.forEach((branch,index) => {
      const conditions = (branch.members ?? []).filter(member => member.type?.literal !== undefined).map(member => ({member:member.name,value:member.type.literal}));
      for (const member of branch.members ?? []) {
        const entries=groups.get(member.name) ?? [];entries.push({member,index,conditions});groups.set(member.name,entries);
      }
    });
    return [...groups.entries()].map(([name,entries]) => {
      const first=entries[0].member, types=entries.map(entry=>entry.member.type);
      const shapes=[...new Set(entries.map(entry=>entry.member.shape))];
      const children=mergeMembers(types);
      return {...first,name,shape:shapes.join(' | '),optional:entries.length<branches.length || entries.some(entry=>entry.member.optional),
        type:types.length===1?types[0]:{kind:'union',name:shapes.join(' | '),variants:types,members:children},members:children,
        branch_variants:entries.map(entry=>({index:entry.index,conditions:entry.conditions,shape:entry.member.shape})),
        evidence:[...new Map(entries.flatMap(entry=>entry.member.evidence ?? []).map(item=>[JSON.stringify(item),item])).values()]};
    });
  }
  function memberType(type, location, subject, depth = 0, seen = new Set()) {
    if (depth >= maxDepth) { gap('type_depth', subject, 'Nested type depth bound reached', evidence(location)); return { kind: 'unknown', reason: 'depth_bound', members: [] }; }
    const name = checker.typeToString(type, location, ts.TypeFormatFlags.NoTruncation);
    if (type.flags & (ts.TypeFlags.Any | ts.TypeFlags.Unknown)) return { kind: 'unknown', name, members: [] };
    if (type.flags & ts.TypeFlags.Boolean) return {kind:'scalar',name:'boolean',members:[]};
    if (type.isUnion()) {
      const branches = type.types.map(branch => memberType(branch, location, subject, depth + 1, new Set(seen)));
      return { kind: 'union', name, variants: branches, members: mergeMembers(branches) };
    }
    if (type.flags & (ts.TypeFlags.StringLike | ts.TypeFlags.NumberLike | ts.TypeFlags.BooleanLike | ts.TypeFlags.BigIntLike | ts.TypeFlags.Null | ts.TypeFlags.Undefined)) return { kind: 'scalar', name, ...(type.isLiteral?.() ? { literal: type.value } : {}), members: [] };
    if (checker.isArrayType(type) || checker.isTupleType(type)) {
      const items = checker.getTypeArguments(type).map(item => memberType(item, location, subject + '[]', depth + 1, new Set(seen)));
      return { kind: 'array', name, items, members: items.flatMap(item => item.members ?? []) };
    }
    if (type.symbol?.name === 'Date') return { kind: 'scalar', name: 'Date', members: [] };
    if (seen.has(type)) { gap('recursive_type', subject, 'Recursive type retains an unresolved continuation', evidence(location)); return { kind: 'unknown', name, reason: 'recursive', members: [] }; }
    seen.add(type);
    const properties = checker.getPropertiesOfType(type);
    const indices = checker.getIndexInfosOfType(type);
    if (properties.length > maxMembers) gap('type_members', subject, 'Type member bound reached', evidence(location));
    const members = properties.slice(0, maxMembers).filter(property => {
      const decl = property.valueDeclaration ?? property.declarations?.[0];
      return decl && texts.has(decl.getSourceFile().fileName) && !ts.isMethodDeclaration(decl) && !ts.isMethodSignature(decl);
    }).map(property => {
      const declaration = property.valueDeclaration ?? property.declarations?.[0];
      const fieldType = checker.getTypeOfSymbolAtLocation(property, declaration ?? location);
      const nested = memberType(fieldType, declaration ?? location, subject + '.' + property.name, depth + 1, new Set(seen));
      return { name: property.name, path: subject + '.' + property.name, shape: checker.typeToString(fieldType, declaration), optional: Boolean(property.flags & ts.SymbolFlags.Optional), type: nested, members: nested.members ?? [], evidence: evidence(declaration ?? location) };
    });
    if (!members.length && !indices.length && properties.length && !properties.some(property => (property.declarations ?? []).some(decl => texts.has(decl.getSourceFile().fileName)))) return { kind: 'unknown', name, reason: 'external_type', members: [] };
    return { kind: 'object', name, members, open: indices.length > 0, ...(indices.length ? { additional_types: indices.map(index => checker.typeToString(index.type)) } : {}) };
  }
  function objectProperties(node, subject, seen = new Set()) {
    if (!node) return [];
    if (ts.isParenthesizedExpression(node) || ts.isAsExpression(node) || ts.isSatisfiesExpression(node)) return objectProperties(node.expression, subject, seen);
    if (ts.isIdentifier(node)) {
      const declaration = symbolAt(node)?.valueDeclaration;
      if (declaration && ts.isVariableDeclaration(declaration) && declaration.initializer && !seen.has(declaration)) { seen.add(declaration); return objectProperties(declaration.initializer, subject, seen); }
      return [];
    }
    if (!ts.isObjectLiteralExpression(node)) return [];
    const result = [];
    for (const property of node.properties) {
      if (ts.isSpreadAssignment(property)) {
        const expanded = objectProperties(property.expression, subject, new Set(seen));
        if (!expanded.length) gap('dynamic_spread', subject, 'Object spread could not be resolved statically', evidence(property));
        result.push(...expanded); continue;
      }
      if (!(ts.isPropertyAssignment(property) || ts.isShorthandPropertyAssignment(property))) { gap('object_member', subject, 'Unsupported object member shape', evidence(property)); continue; }
      const name = keyName(property.name);
      if (!name) { gap('dynamic_key', subject, 'Computed object key could not be resolved statically', evidence(property)); continue; }
      result.push({ name, node: property, value: ts.isPropertyAssignment(property) ? property.initializer : property.name });
    }
    return [...new Map(result.map(property => [property.name, property])).values()];
  }
  for (const sf of syntaxSources) {
    for (const diagnostic of sf.parseDiagnostics ?? []) {
      const start=diagnostic.start??0,end=start+(diagnostic.length??0),first=sf.getLineAndCharacterOfPosition(start),last=sf.getLineAndCharacterOfPosition(end);
      gap('syntax',relative(root,sf.fileName),ts.flattenDiagnosticMessageText(diagnostic.messageText,' '),[{path:relative(root,sf.fileName),sha256:sourceDigest.get(relative(root,sf.fileName)),start,end,start_line:first.line+1,end_line:last.line+1,start_column:first.character+1,end_column:last.character+1,extractor:'typescript-compiler@1'}]);
    }
    walk(sf, node => {
      if (!ts.isVariableDeclaration(node) || !ts.isIdentifier(node.name) || !node.initializer || !ts.isCallExpression(node.initializer)) return;
      const call = node.initializer, origin = callOrigin(call.expression);
      if (!origin || !origin.module.startsWith('drizzle-orm/') || !['pgTable','mysqlTable','sqliteTable'].includes(origin.name)) {
        if(isSchemaWrapper(call.expression)) {gap('schema_wrapper',symbolId(node.name),'Local schema factory requires bounded argument evaluation before its declared fields can be established',evidence(call));return;}
        if ((origin && ['pgTable','mysqlTable','sqliteTable'].includes(origin.name)) || (ts.isIdentifier(call.expression) && ['pgTable','mysqlTable','sqliteTable'].includes(call.expression.text) && !symbolAt(call.expression)?.declarations?.length)) gap('unresolved_schema_constructor',symbolId(node.name),'Schema-looking constructor has no supported import binding',evidence(call));
        return;
      }
      const name = call.arguments[0];
      if (!name || !ts.isStringLiteralLike(name)) { gap('dynamic_table', symbolId(node.name), 'Table name is not a static string', evidence(call)); return; }
      const table = { symbol_id: symbolId(node.name), symbol: node.name.text, name: name.text, source: span(node), evidence: evidence(node), fields: [] };
      for (const property of objectProperties(call.arguments[1], table.symbol_id)) {
        const calls = chain(property.value), initial = calls.at(-1), initialName = initial ? ts.isIdentifier(initial.expression) ? initial.expression.text : nodeText(initial.expression) : '';
        if (!initial) { gap('column_builder', table.symbol_id + '.' + property.name, 'Column builder is not a statically supported call', evidence(property.node)); continue; }
        const physical = initial.arguments[0] && ts.isStringLiteralLike(initial.arguments[0]) ? initial.arguments[0].text : property.name;
        const fieldId = table.symbol_id + '.' + property.name;
        const typed = calls.find(item => ts.isPropertyAccessExpression(item.expression) && item.expression.name.text === '$type' && item.typeArguments?.length);
        let type;
        if (typed) type = memberType(checker.getTypeFromTypeNode(typed.typeArguments[0]), typed.typeArguments[0], physical);
        else {
          const base = callOrigin(initial.expression)?.name ?? initialName;
          type = /^(?:json|jsonb)$/.test(base) ? {kind:'unknown',name:'json',members:[]} : /^(?:text|varchar|char|uuid)$/.test(base) ? {kind:'scalar',name:'string',members:[]} : /^(?:boolean|bool)$/.test(base) ? {kind:'scalar',name:'boolean',members:[]} : /^(?:timestamp|date|datetime|time)$/.test(base) ? {kind:'scalar',name:'Date',members:[]} : /^(?:integer|int|smallint|bigint|real|doublePrecision|numeric|serial|bigserial)$/.test(base) ? {kind:'scalar',name:'number',members:[]} : {kind:'unknown',name:base,members:[]};
          if (calls.some(item => ts.isPropertyAccessExpression(item.expression) && item.expression.name.text === 'array')) type={kind:'array',name:type.name+'[]',items:[type],members:[]};
        }
        const field = { name:physical,property:property.name,field_id:fieldId,shape:nodeText(property.value),type,members:type.members ?? [],foreign_key:null,writes:[],source:span(property.node),evidence:evidence(property.node) };
        const fk = calls.find(item => ts.isPropertyAccessExpression(item.expression) && item.expression.name.text === 'references');
        if (fk) {
          const callback=fk.arguments[0]; let returned=callback && (ts.isArrowFunction(callback)||ts.isFunctionExpression(callback)) ? callback.body : undefined;
          if (returned && ts.isBlock(returned)) returned=returned.statements.find(ts.isReturnStatement)?.expression;
          if (returned && ts.isPropertyAccessExpression(returned)) field.foreign_key={table_symbol_id:symbolId(returned.expression),table:null,column:null,target_property:returned.name.text,evidence:evidence(fk)};
          else gap('foreign_key',fieldId,'Reference callback is not a direct static column reference',evidence(fk));
        }
        for (const member of field.members) fact('declared_member',fieldId,member.path,property.node,{shape:member.shape});
        table.fields.push(field);
      }
      if (!table.fields.length) gap('empty_table',table.symbol_id,'No current fields could be extracted from this declaration',evidence(node));
      tables.push(table); fact('declared_table',table.symbol_id,table.name,node);
    });
  }
  const bySymbol=new Map(tables.map(table=>[table.symbol_id,table]));
  for (const table of tables) for (const field of table.fields) if (field.foreign_key) {
    const target=bySymbol.get(field.foreign_key.table_symbol_id),column=target?.fields.find(item=>item.property===field.foreign_key.target_property);
    if (target && column) {field.foreign_key.table=target.name;field.foreign_key.column=column.name;facts.push({id:hash(field.field_id+'fk').slice(0,24),kind:'foreign_key',subject:field.field_id,object:column.field_id,evidence:field.foreign_key.evidence});}
    else gap('foreign_key_target',field.field_id,'Referenced column is not available in current extracted declarations',field.foreign_key.evidence);
  }
  function valueOf(node, depth=0, seen=new Set()) {
    if (!node || depth>=6) return {kind:'unknown',reason:'lineage_bound'};
    if (ts.isParenthesizedExpression(node)||ts.isAsExpression(node)||ts.isSatisfiesExpression(node)||ts.isAwaitExpression(node)) return valueOf(node.expression,depth+1,seen);
    if (ts.isStringLiteralLike(node)||ts.isNumericLiteral(node)||[ts.SyntaxKind.TrueKeyword,ts.SyntaxKind.FalseKeyword,ts.SyntaxKind.NullKeyword].includes(node.kind)) return {kind:'literal',expression:nodeText(node),literal_type:ts.isStringLiteralLike(node)?'string':ts.isNumericLiteral(node)?'number':node.kind===ts.SyntaxKind.NullKeyword?'null':'boolean',...(ts.isStringLiteralLike(node)?{literal:node.text}:{})};
    if (ts.isObjectLiteralExpression(node)) return {kind:'object',expression:nodeText(node),members:objectProperties(node,'value').map(property=>({name:property.name,value:valueOf(property.value,depth+1,new Set(seen)),evidence:evidence(property.node)}))};
    if (ts.isArrayLiteralExpression(node)) return {kind:'array',items:node.elements.slice(0,maxMembers).map(item=>valueOf(item,depth+1,new Set(seen)))};
    if (ts.isConditionalExpression(node)) return {kind:'union',expression:nodeText(node),variants:[valueOf(node.whenTrue,depth+1,new Set(seen)),valueOf(node.whenFalse,depth+1,new Set(seen))]};
    if (ts.isIdentifier(node)) {
      const symbol=symbolAt(node),declaration=symbol?.valueDeclaration;
      const result={kind:'reference',expression:nodeText(node),source_symbol:symbolId(node),evidence:evidence(declaration ?? node)};
      if (declaration && ts.isVariableDeclaration(declaration) && declaration.initializer && !seen.has(declaration)) {seen.add(declaration);result.origin=valueOf(declaration.initializer,depth+1,seen);}
      if (declaration && ts.isParameter(declaration) && declaration.type) result.type=memberType(checker.getTypeFromTypeNode(declaration.type),declaration.type,node.text);
      return result;
    }
    if (ts.isPropertyAccessExpression(node)) return {kind:'reference',expression:nodeText(node),source_symbol:symbolId(node.expression),source_property:node.name.text,base:valueOf(node.expression,depth+1,seen),evidence:evidence(node)};
    if (ts.isCallExpression(node)) {
      const result={kind:'call',expression:nodeText(node),callee:nodeText(node.expression),source_symbol:symbolId(node.expression),arguments:node.arguments.map(argument=>valueOf(argument,depth+1,new Set(seen))),evidence:evidence(node)};
      if (ts.isPropertyAccessExpression(node.expression)) result.receiver=valueOf(node.expression.expression,depth+1,new Set(seen));
      let declaration=symbolAt(ts.isPropertyAccessExpression(node.expression)?node.expression.name:node.expression)?.valueDeclaration;
      if (declaration && ts.isVariableDeclaration(declaration) && declaration.initializer && (ts.isArrowFunction(declaration.initializer)||ts.isFunctionExpression(declaration.initializer))) declaration=declaration.initializer;
      if (declaration && (ts.isFunctionDeclaration(declaration)||ts.isFunctionExpression(declaration)||ts.isArrowFunction(declaration)||ts.isMethodDeclaration(declaration)) && declaration.body && texts.has(declaration.getSourceFile().fileName) && !seen.has(declaration)) {
        const next=new Set(seen);next.add(declaration);
        const returns=ts.isBlock(declaration.body)?declaration.body.statements.filter(ts.isReturnStatement).map(statement=>statement.expression).filter(Boolean):[declaration.body];
        result.local_returns=returns.map(returned=>valueOf(returned,depth+1,next));result.definition_evidence=evidence(declaration);
        result.parameter_bindings=declaration.parameters.map((parameter,index)=>({parameter:nodeText(parameter.name),argument_index:index}));
      }
      return result;
    }
    if (ts.isBinaryExpression(node)) return {kind:'transform',expression:nodeText(node),operator:ts.tokenToString(node.operatorToken.kind),inputs:[valueOf(node.left,depth+1,new Set(seen)),valueOf(node.right,depth+1,new Set(seen))]};
    return {kind:'unknown',expression:nodeText(node),reason:'unsupported_value_expression',evidence:evidence(node)};
  }
  if (!options.structureOnly) for (const sf of syntaxSources) walk(sf,node=>{
    if (!ts.isCallExpression(node)||!ts.isPropertyAccessExpression(node.expression)||!['values','set'].includes(node.expression.name.text)) return;
    const operation=node.expression.expression;
    if (!ts.isCallExpression(operation)||!ts.isPropertyAccessExpression(operation.expression)||!['insert','update'].includes(operation.expression.name.text)) return;
    const targetNode=operation.arguments[0],table=targetNode&&bySymbol.get(symbolId(targetNode));
    if (!table) return;
    const kind=operation.expression.name.text;
    const payload=node.arguments[0],objects=payload&&ts.isArrayLiteralExpression(payload)?payload.elements:[payload];
    for (const object of objects) {
      const properties=objectProperties(object,table.symbol_id);
      if (!properties.length) {gap('dynamic_write',table.symbol_id,'Write payload is not a statically bounded object',evidence(node));continue;}
      for (const property of properties) {
        const field=table.fields.find(item=>item.property===property.name);
        if (!field) {gap('unknown_write_member',table.symbol_id+'.'+property.name,'Write member is not in extracted table',evidence(property.node));continue;}
        const value=valueOf(property.value),write={kind,target:field.field_id,value,evidence:evidence(property.node)};field.writes.push(write);
        fact('value_write',field.field_id,value.source_symbol,property.node,{operation:kind,value});
        if (value.kind==='unknown') gap('write_value',field.field_id,'Write value has unsupported static lineage',evidence(property.node));
      }
    }
  });
  // Security redaction is independent of privacy classification. Retain facts and source
  // positions while preventing embedded credential literals from entering generated artifacts.
  const securityName = name => /(?:token|secret|password|credential|private_?key|api_?key|bearer|verifier)/i.test(name ?? '');
  function redactText(text, force) {
    const wrapped='('+text+')', sf=ts.createSourceFile('redaction.ts',wrapped,ts.ScriptTarget.Latest,true), replacements=[];
    const inspect=(node,active)=>{
      const sensitive=active || ((ts.isPropertyAssignment(node)||ts.isShorthandPropertyAssignment(node)) && securityName(keyName(node.name)));
      if(sensitive && (ts.isStringLiteralLike(node)||ts.isNumericLiteral(node)||ts.isBigIntLiteral(node)||ts.isRegularExpressionLiteral(node)||ts.isTemplateExpression(node))) {
        const start=node.getStart(sf)-1,end=node.getEnd()-1;
        if(start>=0 && end<=text.length) replacements.push({start,end});
        return;
      }
      ts.forEachChild(node,child=>inspect(child,sensitive));
    };
    inspect(sf,force);
    let redacted=text;
    for(const {start,end} of replacements.sort((a,b)=>b.start-a.start)) redacted=redacted.slice(0,start)+'"<redacted>"'+redacted.slice(end);
    return redacted;
  }
  function redactValue(value, force=false) {
    if(Array.isArray(value)) return value.map(item=>redactValue(item,force));
    if(!value || typeof value!=='object') return value;
    const active=force || securityName(value.name) || securityName(value.property) || securityName(value.source_symbol) || securityName(value.source_property);
    const result={};
    for(const [key,item] of Object.entries(value)) {
      if((key==='literal'||key==='value' && (typeof item==='string'||typeof item==='number')) && active) result[key]='<redacted>';
      else if(typeof item==='string' && ['expression','shape','callee','name'].includes(key)) result[key]=redactText(item,active);
      else result[key]=redactValue(item,active);
    }
    if(active && ('expression' in value || 'literal' in value)) result.redacted=true;
    return result;
  }
  const sensitiveFields=new Set();
  for(const table of tables) table.fields=table.fields.map(field=>{const sensitive=securityName(field.name)||securityName(field.property);if(sensitive)sensitiveFields.add(field.field_id);return redactValue(field,sensitive);});
  for(let index=0;index<facts.length;index++) {const current=facts[index];const redacted=redactValue(current,sensitiveFields.has(current.subject));delete redacted.id;redacted.id=hash(JSON.stringify(redacted)).slice(0,24);facts[index]=redacted;}
  const uniqueSources=[...new Map(sources.map(source=>[source.path,source])).values()].sort((a,b)=>a.path.localeCompare(b.path));
  return {version:1,adapter:ADAPTER,sources:uniqueSources,tables:tables.sort((a,b)=>a.symbol_id.localeCompare(b.symbol_id)),facts:facts.sort((a,b)=>a.id.localeCompare(b.id)),gaps:gaps.sort((a,b)=>(a.subject??'').localeCompare(b.subject??'')||a.kind.localeCompare(b.kind)),coverage:{source_files:syntaxSources.length,table_count:tables.length,field_count:tables.reduce((n,table)=>n+table.fields.length,0),excluded_sources:files.filter(name=>sourceExtension.test(name)&&excludedSource(name)).sort(),migration_support:[...supportSources].sort(),migration_sources:files.filter(name=>recognizedMigrations.has(name)||migrationRoots.some(directory=>name===directory||name.startsWith(directory+'/'))||extraExcluded.has(name)).sort(),migration_roots:[...new Set(migrationRoots)].sort(),limits:{max_files:maxFiles,max_depth:maxDepth,max_members:maxMembers},scope:'Current source declarations and bounded direct writes; migrations, fixtures, generated outputs and dependency implementations are excluded. No runtime execution or external network.'}};
}

if (process.argv[1] && fs.existsSync(process.argv[1]) && fs.realpathSync(process.argv[1])===fs.realpathSync(fileURLToPath(import.meta.url))) {
  try {
    const args=process.argv.slice(2),repo=args.find(arg=>arg.startsWith('--repo='))?.slice(7),output=args.find(arg=>arg.startsWith('--output='))?.slice(9);
    if (!repo || args.some(arg=>!arg.startsWith('--repo=')&&!arg.startsWith('--output='))) throw new Error('usage: typescript_evidence.mjs --repo=<path> [--output=<path>]');
    const result=JSON.stringify(extractRepository(repo),null,2)+'\n';
    if(output) fs.writeFileSync(output,result);else process.stdout.write(result);
  } catch (error) {process.stderr.write((error instanceof Error?error.message:String(error))+'\n');process.exitCode=1;}
}
