"""前后端差异类型（diff kind）契约一致性回归测试。

结构体指纹与命名约定假设（Scoped Fingerprint Assumptions）：
1. 后端构造真实差异字典时，需包含 ``"kind"`` 键，且至少包含
   ``DIFF_FINGERPRINT_KEYS`` 中的任一结构字段或使用 ``**unpack`` 字典解包，
   以排除 ``{"kind": "pdf"}``、``{"kind": "line"}``、``{"kind": "optional_content"}`` 等非 diff 对象。
2. 对已构造差异字典就地修改或 ``.update(kind=...)`` 时，目标变量名需包含 ``"diff"`` 子串。
3. 辅助函数以 ``*args`` 元组或内部发射器回调构造差异时，``kind`` 须可静态解析或显式映射；
   若遇到无法静态解析的动态分支，扫描器采取 Fail-Loud 策略直接抛出 ``AssertionError``。
4. 嵌套具名转发须直接向返回容器 ``.append((...))`` 或直接 ``yield (...)``（不通过中间 tuple 变量）；
   发射器回调须直接传参调用（不经局部别名重绑定）。
"""

import ast
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
BACKEND_DIR = ROOT / "backend"
FRONTEND_API_TS = ROOT / "frontend" / "src" / "api.ts"

# 用于区分 diff 字典与 source/vector/unsupported 等非 diff 字典的固有字段指纹
DIFF_FINGERPRINT_KEYS = {
    "table_id",
    "table_name",
    "severity",
    "old_source",
    "new_source",
    "field",
    "row_key",
}


def _iter_own_nodes(scope_node: ast.AST):
    """仅遍历当前作用域内的 AST 节点，不穿透进入嵌套函数定义。"""
    stack = list(ast.iter_child_nodes(scope_node))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        yield node
        stack.extend(ast.iter_child_nodes(node))


def _eval_strings(node: ast.AST, env: dict[str, set[str]]) -> set[str]:
    """静态求值表达式可能产生的字符串集合（支持常量、局部变量、三元表达式与字典下标）。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, ast.Name):
        return set(env.get(node.id, set()))
    if isinstance(node, ast.IfExp):
        return _eval_strings(node.body, env) | _eval_strings(node.orelse, env)
    if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Dict):
        values: set[str] = set()
        for val_node in node.value.values:
            values |= _eval_strings(val_node, env)
        return values
    return set()


def _collect_local_env(func_node: ast.AST) -> dict[str, set[str]]:
    """收集当前作用域内的简单赋值与元组字面量循环解包绑定。"""
    env: dict[str, set[str]] = {}
    for node in _iter_own_nodes(func_node):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            values = _eval_strings(node.value, env)
            if values:
                env.setdefault(node.targets[0].id, set()).update(values)
        elif (
            isinstance(node, ast.For)
            and isinstance(node.target, ast.Tuple)
            and isinstance(node.iter, (ast.Tuple, ast.List))
        ):
            for idx, elt in enumerate(node.target.elts):
                if not isinstance(elt, ast.Name):
                    continue
                for row in node.iter.elts:
                    if isinstance(row, (ast.Tuple, ast.List)) and idx < len(row.elts):
                        values = _eval_strings(row.elts[idx], env)
                        if values:
                            env.setdefault(elt.id, set()).update(values)
    return env


def _is_diff_dict(node: ast.Dict) -> bool:
    """判断字典字面量是否为差异（diff）记录构造，排除 source/vector/unsupported 等结构。"""
    keys = {k.value for k in node.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)}
    has_unpack = any(k is None for k in node.keys)
    return "kind" in keys and (bool(keys & DIFF_FINGERPRINT_KEYS) or has_unpack)


def _extract_mapping_dict(fn: ast.FunctionDef, var_name: str) -> dict[str, str] | None:
    """识别发射器内部对 kind 参数的字典映射（如 cost_reports.py 中的 business_* 映射）。"""
    for node in _iter_own_nodes(fn):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == var_name
            and isinstance(node.value, ast.Subscript)
            and isinstance(node.value.value, ast.Dict)
        ):
            mapping: dict[str, str] = {}
            for k, v in zip(node.value.value.keys, node.value.value.values):
                assert isinstance(k, ast.Constant) and isinstance(k.value, str), "映射键必须是字符串常量"
                assert isinstance(v, ast.Constant) and isinstance(v.value, str), "映射值必须是字符串常量"
                mapping[k.value] = v.value
            return mapping
    return None


def _resolve_named_arg_node(
    call: ast.Call,
    fn_def: ast.FunctionDef,
    sub_kind_idx: int | None,
    param_name: str,
) -> ast.AST | None:
    """从函数调用节点中按关键字、位置或默认值提取指定形参对应的实参 AST 节点。"""
    for kw in call.keywords:
        if kw.arg == param_name:
            return kw.value
    if sub_kind_idx is not None and sub_kind_idx < len(call.args):
        return call.args[sub_kind_idx]
    if sub_kind_idx is not None:
        pos_count = len(fn_def.args.args)
        defaults = fn_def.args.defaults
        default_offset = sub_kind_idx - (pos_count - len(defaults))
        if 0 <= default_offset < len(defaults):
            return defaults[default_offset]
    for kw_param, kw_default in zip(fn_def.args.kwonlyargs, fn_def.args.kw_defaults):
        if kw_param.arg == param_name and kw_default is not None:
            return kw_default
    return None


def _returned_names(expr: ast.AST | None) -> set[str]:
    """仅从 return 表达式的值位置提取变量名（不扫描条件判断或函数调用参数）。"""
    if expr is None:
        return set()
    if isinstance(expr, ast.Name):
        return {expr.id}
    if isinstance(expr, ast.Starred):
        return _returned_names(expr.value)
    if isinstance(expr, (ast.Tuple, ast.List)):
        names: set[str] = set()
        for elt in expr.elts:
            names |= _returned_names(elt)
        return names
    if isinstance(expr, ast.IfExp):
        return _returned_names(expr.body) | _returned_names(expr.orelse)
    return set()


def _extract_flow_sources(expr: ast.AST, nested_fn_names: set[str]) -> tuple[set[str], set[str]]:
    """从数据流源表达式提取汇入的局部容器变量名与同级嵌套生成器名。"""
    if isinstance(expr, ast.Name):
        return ({expr.id}, set())
    if isinstance(expr, ast.Call) and isinstance(expr.func, ast.Name):
        if expr.func.id in nested_fn_names:
            return (set(), {expr.func.id})
        if expr.func.id in ("list", "tuple") and len(expr.args) == 1:
            return _extract_flow_sources(expr.args[0], nested_fn_names)
    if isinstance(expr, ast.IfExp):
        c1, g1 = _extract_flow_sources(expr.body, nested_fn_names)
        c2, g2 = _extract_flow_sources(expr.orelse, nested_fn_names)
        return (c1 | c2, g1 | g2)
    return (set(), set())


def _extract_from_tuple_helper(helper: ast.FunctionDef, kind_idx: int) -> set[str]:
    """从返回参数元组的辅助函数（如 _content_changes）中提取第 kind_idx 位差异类型。"""
    helper_env = _collect_local_env(helper)
    results: set[str] = set()
    nested_fns = [n for n in ast.walk(helper) if isinstance(n, ast.FunctionDef) and n is not helper]
    nested_fn_names = {hf.name for hf in nested_fns}

    # 计算各作用域的局部变量环境（嵌套函数剔除自身形参对同名外层变量的遮蔽）
    scope_envs: dict[int, dict[str, set[str]]] = {id(helper): helper_env}
    for hf in nested_fns:
        hf_param_names = {a.arg for a in hf.args.args} | {a.arg for a in hf.args.kwonlyargs}
        if hf.args.vararg is not None:
            hf_param_names.add(hf.args.vararg.arg)
        if hf.args.kwarg is not None:
            hf_param_names.add(hf.args.kwarg.arg)
        merged_env = {k: set(v) for k, v in helper_env.items() if k not in hf_param_names}
        for k, v in _collect_local_env(hf).items():
            merged_env.setdefault(k, set()).update(v)
        scope_envs[id(hf)] = merged_env

    # 1) 基于明确返回位置与顶层 yield from 初始化结果容器与结果生成器
    result_containers: set[str] = set()
    result_generators: set[str] = set()
    for node in _iter_own_nodes(helper):
        if isinstance(node, ast.Return):
            result_containers |= _returned_names(node.value)
        elif isinstance(node, ast.YieldFrom):
            src_c, src_g = _extract_flow_sources(node.value, nested_fn_names)
            result_containers |= src_c
            result_generators |= src_g

    # 2) 不动点传播显式汇入关系（extend / += / 赋值别名 / for-append / for-yield / yield from）
    flow_changed = True
    while flow_changed:
        flow_changed = False
        for scope_fn in [helper, *nested_fns]:
            is_result_gen_scope = scope_fn is helper or scope_fn.name in result_generators
            for node in _iter_own_nodes(scope_fn):
                src_c: set[str] = set()
                src_g: set[str] = set()
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "extend"
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id in result_containers
                    and len(node.args) == 1
                ):
                    src_c, src_g = _extract_flow_sources(node.args[0], nested_fn_names)
                elif (
                    isinstance(node, ast.AugAssign)
                    and isinstance(node.op, ast.Add)
                    and isinstance(node.target, ast.Name)
                    and node.target.id in result_containers
                ):
                    src_c, src_g = _extract_flow_sources(node.value, nested_fn_names)
                elif (
                    isinstance(node, ast.Assign)
                    and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)
                    and node.targets[0].id in result_containers
                ):
                    src_c, src_g = _extract_flow_sources(node.value, nested_fn_names)
                elif isinstance(node, ast.For) and isinstance(node.target, ast.Name):
                    loop_var = node.target.id
                    feeds_result = False
                    for sub in _iter_own_nodes(node):
                        if (
                            isinstance(sub, ast.Call)
                            and isinstance(sub.func, ast.Attribute)
                            and sub.func.attr == "append"
                            and isinstance(sub.func.value, ast.Name)
                            and sub.func.value.id in result_containers
                            and len(sub.args) == 1
                            and isinstance(sub.args[0], ast.Name)
                            and sub.args[0].id == loop_var
                        ):
                            feeds_result = True
                            break
                        if (
                            is_result_gen_scope
                            and isinstance(sub, ast.Yield)
                            and isinstance(sub.value, ast.Name)
                            and sub.value.id == loop_var
                        ):
                            feeds_result = True
                            break
                    if feeds_result:
                        src_c, src_g = _extract_flow_sources(node.iter, nested_fn_names)
                elif is_result_gen_scope and isinstance(node, ast.YieldFrom):
                    src_c, src_g = _extract_flow_sources(node.value, nested_fn_names)

                if src_c - result_containers:
                    result_containers |= src_c
                    flow_changed = True
                if src_g - result_generators:
                    result_generators |= src_g
                    flow_changed = True

    # 3) 仅识别写入 result_containers 或属于 result_generators 的内部转发函数
    forwarders: dict[str, list[tuple[str, int | None, str | None, ast.FunctionDef]]] = {}
    forwarded_tuple_ids: set[int] = set()

    for hf in nested_fns:
        if hf.args.vararg is not None:
            vararg_name = hf.args.vararg.arg
            writes_result = False
            for node in _iter_own_nodes(hf):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "append"
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id in result_containers
                    and len(node.args) == 1
                    and any(isinstance(n, ast.Name) and n.id == vararg_name for n in ast.walk(node.args[0]))
                ):
                    writes_result = True
                    break
                if (
                    hf.name in result_generators
                    and isinstance(node, ast.Yield)
                    and node.value is not None
                    and any(isinstance(n, ast.Name) and n.id == vararg_name for n in ast.walk(node.value))
                ):
                    writes_result = True
                    break
            if writes_result:
                forwarders.setdefault(hf.name, []).append(("vararg", kind_idx, None, hf))
            continue

        pos_params = [a.arg for a in hf.args.args]
        kwonly_params = [a.arg for a in hf.args.kwonlyargs]
        all_param_names = set(pos_params) | set(kwonly_params)
        param_aliases: dict[str, str] = {}
        for node in _iter_own_nodes(hf):
            if (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and isinstance(node.value, ast.Name)
            ):
                src = param_aliases.get(node.value.id, node.value.id)
                if src in all_param_names:
                    param_aliases[node.targets[0].id] = src
        for node in _iter_own_nodes(hf):
            tup: ast.Tuple | None = None
            is_target_write = False
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "append"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in result_containers
                and len(node.args) == 1
                and isinstance(node.args[0], ast.Tuple)
            ):
                tup = node.args[0]
                is_target_write = True
            elif (
                hf.name in result_generators
                and isinstance(node, ast.Yield)
                and isinstance(node.value, ast.Tuple)
            ):
                tup = node.value
                is_target_write = True
            if not is_target_write or tup is None or any(isinstance(e, ast.Starred) for e in tup.elts) or kind_idx >= len(tup.elts):
                continue
            elt = tup.elts[kind_idx]
            if isinstance(elt, ast.Name):
                resolved_param = param_aliases.get(elt.id, elt.id)
                if resolved_param in all_param_names:
                    sub_idx = pos_params.index(resolved_param) if resolved_param in pos_params else None
                    entry = ("named", sub_idx, resolved_param, hf)
                    if entry not in forwarders.get(hf.name, []):
                        forwarders.setdefault(hf.name, []).append(entry)
                    forwarded_tuple_ids.add(id(tup))

    # 提取全部合法 kind，并对结果容器/生成器/转发调用中的动态不可解析分支执行 Fail-Loud 断言
    for scope_fn in [helper, *nested_fns]:
        env = scope_envs[id(scope_fn)]
        for node in _iter_own_nodes(scope_fn):
            # 1) 内部转发调用（*args 变参或具名参数转发）
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in forwarders:
                for mode, sub_idx, param_name, hf_def in forwarders[node.func.id]:
                    if mode == "vararg":
                        assert kind_idx < len(node.args), f"{helper.name} 中 {node.func.id}() 缺少第 {kind_idx} 位 kind 参数"
                        vals = _eval_strings(node.args[kind_idx], env)
                        assert vals, f"{helper.name} 中 {node.func.id}() 的 kind 参数无法静态解析"
                        results |= vals
                    else:
                        assert param_name is not None
                        arg_node = _resolve_named_arg_node(node, hf_def, sub_idx, param_name)
                        vals = _eval_strings(arg_node, env) if arg_node is not None else set()
                        assert vals, f"{helper.name} 中 {node.func.id}() 的 kind 参数 {param_name!r} 无法静态解析"
                        results |= vals
            # 2) 直接 results.append((category, kind, ...))
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "append"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in result_containers
                and len(node.args) == 1
                and isinstance(node.args[0], ast.Tuple)
            ):
                tup = node.args[0]
                if not any(isinstance(e, ast.Starred) for e in tup.elts) and id(tup) not in forwarded_tuple_ids:
                    assert kind_idx < len(tup.elts), (
                        f"{helper.name} 中 {node.func.value.id}.append(...) 元组长度不足 {kind_idx + 1}"
                    )
                    vals = _eval_strings(tup.elts[kind_idx], env)
                    assert vals, f"{helper.name} 中 {node.func.value.id}.append(...) 的第 {kind_idx} 位 kind 无法静态解析"
                    results |= vals
            # 3) 生成器 yield (category, kind, ...)
            elif (
                isinstance(node, ast.Yield)
                and isinstance(node.value, ast.Tuple)
                and (scope_fn is helper or scope_fn.name in result_generators)
            ):
                tup = node.value
                if not any(isinstance(e, ast.Starred) for e in tup.elts) and id(tup) not in forwarded_tuple_ids:
                    assert kind_idx < len(tup.elts), f"{helper.name} 中 yield 元组长度不足 {kind_idx + 1}"
                    vals = _eval_strings(tup.elts[kind_idx], env)
                    assert vals, f"{helper.name} 中 yield 元组的第 {kind_idx} 位 kind 无法静态解析"
                    results |= vals
    return results


def scan_backend_diff_kinds(backend_dir: pathlib.Path = BACKEND_DIR) -> set[str]:
    """静态扫描 backend/*.py 中所有真实 diff 字典生成、映射与更新的 kind 集合。"""
    found: set[str] = set()
    for path in sorted(backend_dir.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        ancestors: dict[int, list[ast.AST]] = {}

        def index_tree(node: ast.AST, chain: list[ast.AST]) -> None:
            ancestors[id(node)] = chain
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    index_tree(child, [*chain, child])
                else:
                    index_tree(child, chain)

        index_tree(tree, [tree])
        all_funcs = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
        # (父作用域节点 id, 函数名) -> (kind 参数下标, 参数名, 可选映射字典)
        scoped_emitters: dict[tuple[int, str], tuple[int, str, dict[str, str] | None]] = {}

        # 第一遍：识别模块顶层及各函数体内的直接 diff 构造、就地更新与内部发射器函数
        scopes: list[ast.AST] = [tree, *[n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]]
        for scope in scopes:
            env = _collect_local_env(scope)
            params = [a.arg for a in scope.args.args] if isinstance(scope, ast.FunctionDef) else []
            for node in _iter_own_nodes(scope):
                if isinstance(node, ast.Dict) and _is_diff_dict(node):
                    idx_k = next(i for i, k in enumerate(node.keys) if isinstance(k, ast.Constant) and k.value == "kind")
                    val_node = node.values[idx_k]
                    if isinstance(scope, ast.FunctionDef) and isinstance(val_node, ast.Name) and val_node.id in params:
                        parent_scope = ancestors[id(scope)][-2]
                        mapping = _extract_mapping_dict(scope, val_node.id)
                        scoped_emitters[(id(parent_scope), scope.name)] = (
                            params.index(val_node.id),
                            val_node.id,
                            mapping,
                        )
                    else:
                        vals = _eval_strings(val_node, env)
                        assert vals, f"{path.name}:{node.lineno} diff 字典的 kind 无法静态解析"
                        found |= vals
                elif isinstance(node, ast.Assign):
                    for target in node.targets:
                        if (
                            isinstance(target, ast.Subscript)
                            and isinstance(target.value, ast.Name)
                            and "diff" in target.value.id
                            and isinstance(target.slice, ast.Constant)
                            and target.slice.value == "kind"
                        ):
                            vals = _eval_strings(node.value, env)
                            assert vals, f"{path.name}:{node.lineno} diff['kind'] 赋值无法静态解析"
                            found |= vals
                elif (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "update"
                    and isinstance(node.func.value, ast.Name)
                    and "diff" in node.func.value.id
                ):
                    for kw in node.keywords:
                        if kw.arg == "kind":
                            vals = _eval_strings(kw.value, env)
                            assert vals, f"{path.name}:{node.lineno} diff.update(kind=...) 无法静态解析"
                            found |= vals

        def resolve_emitter(scope_node: ast.AST, name: str):
            for ancestor in reversed(ancestors[id(scope_node)]):
                if (id(ancestor), name) in scoped_emitters:
                    return scoped_emitters[(id(ancestor), name)]
            return None

        # 第二遍：不动点迭代追踪将发射器作为回调参数跨多跳传入同模块内部辅助函数的场景
        all_fn_defs = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
        changed = True
        while changed:
            changed = False
            for fn in all_fn_defs:
                for node in _iter_own_nodes(fn):
                    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in all_funcs:
                        callee = all_funcs[node.func.id]
                        callee_params = [a.arg for a in callee.args.args]
                        for idx, arg in enumerate(node.args):
                            if isinstance(arg, ast.Name) and idx < len(callee_params):
                                spec = resolve_emitter(fn, arg.id)
                                key = (id(callee), callee_params[idx])
                                if spec and key not in scoped_emitters:
                                    scoped_emitters[key] = spec
                                    changed = True
                        for kw in node.keywords:
                            if isinstance(kw.value, ast.Name) and kw.arg:
                                spec = resolve_emitter(fn, kw.value.id)
                                key = (id(callee), kw.arg)
                                if spec and key not in scoped_emitters:
                                    scoped_emitters[key] = spec
                                    changed = True

        # 第三遍：解析对全部发射器的调用实参
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef):
                continue
            env = _collect_local_env(fn)
            for node in _iter_own_nodes(fn):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
                    continue
                spec = resolve_emitter(fn, node.func.id)
                if not spec:
                    continue
                kind_idx, kind_name, mapping = spec
                raw_vals: set[str] = set()
                if any(isinstance(a, ast.Starred) for a in node.args):
                    starred = [a.value for a in node.args if isinstance(a, ast.Starred)]
                    assert len(starred) == 1 and isinstance(starred[0], ast.Name), (
                        f"{path.name}:{node.lineno} 无法解析 *args 解包调用"
                    )
                    var_name = starred[0].id
                    matched_helper = False
                    for sub in _iter_own_nodes(fn):
                        if (
                            isinstance(sub, ast.For)
                            and isinstance(sub.target, ast.Name)
                            and sub.target.id == var_name
                            and isinstance(sub.iter, ast.Call)
                            and isinstance(sub.iter.func, ast.Name)
                            and sub.iter.func.id in all_funcs
                        ):
                            matched_helper = True
                            raw_vals |= _extract_from_tuple_helper(all_funcs[sub.iter.func.id], kind_idx)
                    assert matched_helper and raw_vals, f"{path.name}:{node.lineno} *args 调用未提取到任何 kind"
                else:
                    if kind_idx < len(node.args):
                        raw_vals |= _eval_strings(node.args[kind_idx], env)
                    for kw in node.keywords:
                        if kw.arg == kind_name:
                            raw_vals |= _eval_strings(kw.value, env)
                    assert raw_vals, f"{path.name}:{node.lineno} {node.func.id}() 调用的 kind 无法静态解析"

                if mapping is not None:
                    for val in raw_vals:
                        assert val in mapping, f"{path.name}:{node.lineno} kind={val!r} 未在映射表中定义"
                        found.add(mapping[val])
                else:
                    found |= raw_vals
    return found


def _split_top_level_comma(body: str) -> list[str]:
    """按引号外且括号深度为 0 的顶层逗号切分对象条目，避免标签文本含逗号时误判。"""
    segments: list[str] = []
    start = 0
    in_str: str | None = None
    brace_depth = 0
    i = 0
    n = len(body)
    while i < n:
        ch = body[i]
        if in_str:
            if ch == "\\":
                i += 2
                continue
            if ch == in_str:
                in_str = None
        else:
            if ch in ("'", '"', "`"):
                in_str = ch
            elif ch in ("{", "[", "("):
                brace_depth += 1
            elif ch in ("}", "]", ")"):
                brace_depth = max(0, brace_depth - 1)
            elif ch == "," and brace_depth == 0:
                seg = body[start:i].strip()
                if seg:
                    segments.append(seg)
                start = i + 1
        i += 1
    tail = body[start:].strip()
    if tail:
        segments.append(tail)
    return segments


def _strip_ts_comments(text: str) -> str:
    """剥离 TypeScript 源码中引号外的单行与块级注释，保留字符串字面量原样。"""
    cleaned: list[str] = []
    i, n = 0, len(text)
    in_str: str | None = None
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if in_str:
            cleaned.append(ch)
            if ch == "\\" and i + 1 < n:
                cleaned.append(text[i + 1])
                i += 2
                continue
            if ch == in_str:
                in_str = None
            i += 1
        else:
            if ch == "/" and nxt == "/":
                i += 2
                while i < n and text[i] != "\n":
                    i += 1
                continue
            if ch == "/" and nxt == "*":
                i += 2
                while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                    i += 1
                i = i + 2 if i + 1 < n else n
                # 使用单空格占位块注释，防止两侧标识符或关键字粘连
                cleaned.append(" ")
                continue
            if ch in ("'", '"', "`"):
                in_str = ch
            cleaned.append(ch)
            i += 1
    return "".join(cleaned)


def extract_frontend_kind_labels(ts_path: pathlib.Path = FRONTEND_API_TS) -> dict[str, str]:
    """先剥离全文注释，再使用括号配对与字符串状态机从 frontend/src/api.ts 提取 kindLabels 字典。"""
    raw_text = ts_path.read_text(encoding="utf-8")
    cleaned_text = _strip_ts_comments(raw_text)
    match = re.search(r"export\s+const\s+kindLabels\b[^=]*=\s*\{", cleaned_text)
    assert match, f"未在 {ts_path} 中找到 export const kindLabels 定义"

    start = match.end() - 1
    i, n, depth = start, len(cleaned_text), 0
    in_str: str | None = None
    end: int | None = None

    while i < n:
        ch = cleaned_text[i]
        if in_str:
            if ch == "\\":
                i += 1
            elif ch == in_str:
                in_str = None
        else:
            if ch in ("'", '"', "`"):
                in_str = ch
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        i += 1

    assert end is not None, "frontend/src/api.ts 中 kindLabels 对象字面量花括号未闭合"
    cleaned_body = cleaned_text[start + 1 : end - 1]
    raw_items = _split_top_level_comma(cleaned_body)
    entry_pattern = re.compile(
        r"\s*(?:([A-Za-z_][A-Za-z0-9_]*)|'([A-Za-z_][A-Za-z0-9_]*)'|\"([A-Za-z_][A-Za-z0-9_]*)\")"
        r"\s*:\s*('(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\")\s*",
        re.S,
    )

    labels: dict[str, str] = {}
    for item in raw_items:
        item_match = entry_pattern.fullmatch(item)
        assert item_match is not None, f"kindLabels 存在无法解析的条目: {item!r}"
        k1, k2, k3, raw_val = item_match.groups()
        key = k1 or k2 or k3
        assert key not in labels, f"kindLabels 存在重复键: {key}"
        val = raw_val[1:-1].strip()
        assert val, f"kindLabels[{key!r}] 的展示文案为空"
        labels[key] = val
    return labels


def test_backend_diff_kinds_match_frontend_kind_labels():
    """验证后端实际生成的全部差异 kind 均在前端 kindLabels 中注册且无死键。"""
    backend_kinds = scan_backend_diff_kinds()
    frontend_labels = extract_frontend_kind_labels()
    frontend_kinds = set(frontend_labels)

    # 防止扫描规则意外退化漏扫
    assert len(backend_kinds) >= 57, f"后端扫描结果数量异常偏少: {len(backend_kinds)}"

    missing_in_frontend = sorted(backend_kinds - frontend_kinds)
    dead_in_frontend = sorted(frontend_kinds - backend_kinds)

    assert not missing_in_frontend, f"frontend/src/api.ts kindLabels 缺少后端差异类型: {missing_in_frontend}"
    assert not dead_in_frontend, f"frontend/src/api.ts kindLabels 存在后端未生成的死键: {dead_in_frontend}"
    assert "report_uncertain" not in frontend_kinds
