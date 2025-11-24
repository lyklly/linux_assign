import os

def extract_mount_to_relations(
    root_node,
    code_bytes,
    function_id_map,
    variable_id_map,
    field_id_map,
    current_file_path,
    file_visibility,
    entity_file_map,
    extern_functions=None,
    macro_lookup_map=None,
    file_path=None,
    flag=False
):
    """
    基于文件可见性的赋值关系提取
    支持多值映射的变量查找，正确处理同名全局变量消歧
    新增：支持结构体初始化器中的字段赋值
    """
    def get_text(node):
        return code_bytes[node.start_byte:node.end_byte].decode("utf-8", errors="ignore")

    def resolve_name_with_visibility(name, current_scope, visible_files):
        """基于可见性解析名称到实体ID，支持多值映射"""

        candidates = []
        
        # 1. 检查局部变量
        local_var_key = (name, current_scope)
        if local_var_key in variable_id_map:
            var_id_or_list = variable_id_map[local_var_key]
            var_ids = var_id_or_list if isinstance(var_id_or_list, list) else [var_id_or_list]
            
            for var_id in var_ids:
                var_file = entity_file_map.get(var_id)
                if var_file and var_file in visible_files:
                    priority = 0
                    candidates.append((var_id, "local_variable", priority, var_file))
        
        # 2. 检查全局变量
        global_var_key = (name, 'global')
        if global_var_key in variable_id_map:
            var_id_or_list = variable_id_map[global_var_key]
            var_ids = var_id_or_list if isinstance(var_id_or_list, list) else [var_id_or_list]
            
            for var_id in var_ids:
                var_file = entity_file_map.get(var_id)
                if var_file and var_file in visible_files:
                    priority = 0 if var_file == current_file_path else 10
                    candidates.append((var_id, "global_variable", priority, var_file))
        
        # 3. 检查函数
        if name in function_id_map:
            func_ids = function_id_map[name]
            if not isinstance(func_ids, list):
                func_ids = [func_ids]
            
            for func_id in func_ids:
                func_file = entity_file_map.get(func_id)
                if func_file and func_file in visible_files:
                    priority = 0 if func_file == current_file_path else 1
                    candidates.append((func_id, "function", priority, func_file))
        
        # 4. 检查字段
        if name in field_id_map:
            field_ids = field_id_map[name]
            if not isinstance(field_ids, list):
                field_ids = [field_ids]
                
            for field_id in field_ids:
                field_file = entity_file_map.get(field_id)
                if field_file and field_file in visible_files:
                    priority = 0 if field_file == current_file_path else 1
                    candidates.append((field_id, "field", priority, field_file))
        
        if candidates:
            candidates.sort(key=lambda x: x[2])
            return candidates[0][0]
        
        return None

    def find_identifier(node):
        if node is None or node.type == 'pointer_expression':
            return None
        if node.type == 'identifier':
            return node
        for child in node.children:
            result = find_identifier(child)
            if result:
                return result
        return None

    def find_field_identifier(node):
        if node is None:
            return None
        if node.type == 'field_identifier':
            return node
        for child in node.children:
            result = find_field_identifier(child)
            if result:
                return result
        return None

    mount_relations = []

    def traverse(node, current_scope='global'):
        # 进入函数定义
        if node.type == 'function_definition':
            declarator = node.child_by_field_name('declarator')
            func_node = find_identifier(declarator)
            if func_node:
                current_scope = get_text(func_node).strip()

        if node.type == 'call_expression' and 'DELAYED_WORK' in get_text(node):
            arg_node = None
            for child in node.children:
                if child.type == 'argument_list':
                    arg_node = child
                    break
            field_node = find_field_identifier(node)
            func_node = find_identifier(arg_node)

            field_name = get_text(field_node).strip()
            visible_files = file_visibility.get(current_file_path, {current_file_path})
            field_id = resolve_name_with_visibility(field_name, current_scope, visible_files)

            func_name = get_text(func_node).strip()
            func_id = resolve_name_with_visibility(func_name, current_scope, visible_files)

            if field_id and func_id:
                relation = {
                    "head": field_id,
                    "tail": func_id,
                    "type": "MOUNTED_TO",
                    "scope": current_scope,
                    "visibility_checked": True
                }
                
                if relation not in mount_relations:
                    mount_relations.append(relation)
                return
        # 递归遍历
        for child in node.children:
            traverse(child, current_scope)

    traverse(root_node)
    return mount_relations