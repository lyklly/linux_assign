import os
import json
import time
import tracemalloc
from collections import defaultdict, Counter
from tqdm import tqdm
from tree_sitter import Language, Parser
import tree_sitter_c as tsc
import sys
import gc
sys.setrecursionlimit(100000)
# === 实体提取模块 ===
from extract_entity_file import extract_file_entity
from extract_entity_variable import extract_variable_entities, extract_function_parameters
from extract_entity_function import extract_function_entities
from extract_entity_struct import extract_struct_entities
from extract_entity_field import extract_field_entities
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
import pickle
# === 关系提取模块 ===
from extract_relation_calls import extract_calls_relations
from extract_relation_assignedto import extract_assigned_to_relations
from extract_relation_contains import build_file_level_contains
from extract_relation_has_members import extract_has_member_relations
from extract_relation_has_parameters import extract_has_parameter_relations
from extract_relation_has_variables import extract_has_variable_relations
from extract_relation_returns import extract_returns_relations
from extract_relation_typeof import extract_typeof_relations
from extract_fail_message import extarct_mes
from extract_relation_alias import extract_alias_relations
# === 包含关系提取模块 ===
from extract_relation_includes import extract_include_relations, build_transitive_includes, extract_extern_declarations

# === 配置路径 ===
ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
LANG_SO_PATH = os.path.join(ROOT_DIR, '..', 'build', 'my-languages.so')
OUTPUT_BASE = os.path.join(ROOT_DIR, '..', 'output')
MACRO_JSON_PATH = r"E:\cpppro\clang_kg\test\code_kg_with_tree-sitter\output\linux\macro_win.json"

def id_generator(start=1):
    while True:
        yield start
        start += 1

def get_parser():
    language = Language(tsc.language())
    parser = Parser(language)
    return parser

def get_c_files(directory):
    for root, _, files in os.walk(directory):
        for file in files:
            if file.lower().endswith(('.c', '.h')):
                yield os.path.join(root, file)

def load_macro_lookup_map(json_path):
    if not os.path.exists(json_path):
        print(f"Warning: Macro file not found: {json_path}")
        return defaultdict(list)
        
    with open(json_path, 'r') as f:
        macro_json = json.load(f)
    macro_lookup_map = defaultdict(list)
    
    # 🔧 修复：获取 macro.json 的目录作为基础路径
    macro_dir = os.path.dirname(json_path)
    
    for entry in macro_json:
        file = entry["file"]  # "./test_1.c" 
        start_line, start_col, end_line, end_col = entry["location"]
        macro_lookup_map[file].append({
            "range": ((start_line, start_col), (end_line, end_col)),
            "expanded": entry["macro"],
            "original": entry["name"],
            'extracted_lines': entry['extracted_lines']
        })
    return macro_lookup_map

def build_entity_file_mapping(all_entities):
    """构建实体ID到文件路径的映射"""
    entity_file_map = {}
    
    for entity in all_entities:
        if entity.get('source_file'):
            abs_path = os.path.abspath(entity['source_file'])
            entity_file_map[entity['id']] = abs_path
        elif entity.get('type') == 'FILE':
            if entity.get('source_file'):
                abs_path = os.path.abspath(entity['source_file'])
            else:
                abs_path = os.path.abspath(entity['name'])
            entity_file_map[entity['id']] = abs_path
    
    return entity_file_map

def build_file_to_entities_mapping(all_entities):
    """🚀 新增：构建文件到实体的映射，用于快速查找"""
    file_to_entities = defaultdict(list)
    
    for entity in all_entities:
        if entity.get('source_file'):
            abs_path = os.path.abspath(entity['source_file'])
            file_to_entities[abs_path].append(entity)
    
    return file_to_entities

# ========== 优化1：减少数据传递，使用全局变量（适用于多进程） ==========
_GLOBAL_SHARED_DATA = None

def init_worker(shared_data_path):
    """进程初始化函数：从磁盘加载共享数据"""
    global _GLOBAL_SHARED_DATA
    with open(shared_data_path, 'rb') as f:
        _GLOBAL_SHARED_DATA = pickle.load(f)


# ========== 优化2：轻量级工作函数（只传文件路径） ==========
def process_calls_worker(source_path):
    """阶段4：CALLS关系提取（轻量级版本）"""
    parser = get_parser()
    abs_source_path = os.path.abspath(source_path)
    
    try:
        with open(abs_source_path, 'rb') as f:
            code_bytes = f.read()
        tree = parser.parse(code_bytes)
        root = tree.root_node
        
        shared = _GLOBAL_SHARED_DATA
        rels = extract_calls_relations(
            root, code_bytes, 
            shared['function_id_map'], 
            shared['var_param_map'],
            shared['field_id_map'],
            source_path, 
            shared['file_visibility'], 
            shared['entity_file_map'], 
            shared['all_extern_functions'], 
            shared['macro_lookup_map'], 
            source_path, 
            shared['all_entities'],
            flag=True
        )
        gc.collect()
        del tree, root
        return rels
    except Exception as e:
        print(f"Error in {source_path}: {e}")
        return []

def process_assigned_worker(source_path):
    """阶段5：ASSIGNED_TO关系提取"""
    parser = get_parser()
    abs_source_path = os.path.abspath(source_path)
    
    try:
        with open(abs_source_path, 'rb') as f:
            code_bytes = f.read()
        tree = parser.parse(code_bytes)
        root = tree.root_node
        
        shared = _GLOBAL_SHARED_DATA
        rels = extract_assigned_to_relations(
            root, code_bytes,
            shared['function_id_map'],
            shared['var_param_map'],
            shared['field_id_map'],
            source_path,
            shared['file_visibility'],
            shared['entity_file_map'],
            shared['all_extern_functions'],
            shared['macro_lookup_map'],
            source_path,
            flag=True
        )
        
        del tree, root
        return rels
    except Exception as e:
        print(f"Error in {source_path}: {e}")
        return []

def init_worker(var_ent, par_ent, fld_ent, str_map, file_vis, ent_map):
    """
    在每个子进程启动时初始化全局变量
    这个函数会在每个工作进程创建时被调用一次
    """
    global variable_entities, param_entities, field_entities
    global struct_id_map, file_visibility, entity_file_map
    
    variable_entities = var_ent
    param_entities = par_ent
    field_entities = fld_ent
    struct_id_map = str_map
    file_visibility = file_vis
    entity_file_map = ent_map


def parallel_extract_with_threads(c_files, shared_data, process_func, stage_name, num_workers=8):
    """使用线程池并行处理（适合I/O密集型任务）"""
    print(f"\n{'='*60}")
    print(f"{stage_name} (使用 {num_workers} 个线程)")
    
    # 设置全局共享数据（线程间共享内存，无序列化开销）
    global _GLOBAL_SHARED_DATA
    _GLOBAL_SHARED_DATA = shared_data
    
    all_rels = []
    
    with ThreadPoolExecutor(max_workers=num_workers) as executor:
        # 提交所有任务
        futures = {executor.submit(process_func, f): f for f in c_files}
        
        # 使用 tqdm 显示进度
        with tqdm(total=len(c_files), desc=stage_name) as pbar:
            for future in as_completed(futures):
                rels = future.result()
                all_rels.extend(rels)
                pbar.update(1)
    
    print(f"✅ {stage_name} 完成，提取到 {len(all_rels)} 条关系")
    return all_rels


def deduplicate_relations(relations):
    """去重关系列表"""
    seen = set()
    unique_relations = []
    
    for rel in relations:
        rel_key = (rel['head'], rel['tail'], rel['type'])
        if rel_key not in seen:
            seen.add(rel_key)
            unique_relations.append(rel)
    
    return unique_relations

def extract_all(source_dir, output_dir):
    os.makedirs(output_dir, exist_ok=True)
    entity_path = os.path.join(output_dir, 'entity.json')
    relation_path = os.path.join(output_dir, 'relation.json')

    id_counter = id_generator()
    parser = get_parser()

    all_entities = []
    all_relations = []

    # === 映射表：支持多值映射 ===
    function_id_map = {}      # name -> [id1, id2, ...] 支持同名函数
    variable_id_map = {}      # (name, scope) -> id 或 [id1, id2, ...] 支持同名全局变量
    param_id_map = {}         # (name, scope) -> id 
    struct_id_map = {}        # (name, scope) -> [id1, id2, ...] 支持同名结构体
    field_id_map = {}         # name -> [id1, id2, ...] 
    variable_scope_map = {}

    function_entities = []
    param_entities = []
    variable_entities = []
    struct_entities = []
    field_entities = []

    file_trees = []
    file_id_map = {}

    # === 源码与宏信息读取 ===
    c_files = list(get_c_files(source_dir))
    with open(r'E:\cpppro\clang_kg\test\code_kg_with_tree-sitter\output\linux\dupfile.json', 'r', encoding='utf-8') as f:
        dup_file = json.load(f)

    cal_files = []
    for value in c_files:
        if value not in dup_file:
            cal_files.append(value)
    c_files = cal_files
    macro_lookup_map = load_macro_lookup_map(MACRO_JSON_PATH)
    print(f"✅ 读取宏展开信息完成，共包含文件数：{len(macro_lookup_map)}")

    # === 阶段 1：提取所有实体 ===
    print(f"\n" + "="*60)
    print("阶段 1：提取所有实体")

    with open(r'E:\cpppro\clang_kg\test\code_kg_with_tree-sitter\output\linux\res\temp_en.json', 'r', encoding='utf-8') as f:
        all_entities = json.load(f)
    import pickle
    data_to_save = pickle.load(open(r'E:\cpppro\clang_kg\test\code_kg_with_tree-sitter\output\linux\name2id.pkl', 'rb'))
    function_id_map = data_to_save['function_id_map']
    variable_id_map = data_to_save['variable_id_map']
    param_id_map = data_to_save['param_id_map']
    struct_id_map = data_to_save['struct_id_map']
    field_id_map = data_to_save['field_id_map']
    variable_scope_map = data_to_save['variable_scope_map']
    file_id_map = data_to_save['file_id_map']
    entity_file_map = data_to_save['entity_file_map']
    file_visibility = data_to_save['file_visibility']
    all_extern_functions = data_to_save['all_extern_functions']
    all_include_relations = data_to_save['all_include_relations']
    function_entities = data_to_save['function_entities']
    param_entities = data_to_save['param_entities']
    variable_entities = data_to_save['variable_entities']
    struct_entities = data_to_save['struct_entities']
    field_entities = data_to_save['field_entities']


    var_param_map = {**variable_id_map, **param_id_map}
    
    shared_data = {
        'function_id_map': function_id_map,
        'var_param_map': var_param_map,
        'field_id_map': field_id_map,
        'struct_id_map': struct_id_map,
        'file_visibility': file_visibility,
        'entity_file_map': entity_file_map,
        'all_extern_functions': all_extern_functions,
        'macro_lookup_map': macro_lookup_map,
        'all_entities': all_entities,
        'var_param_entities': variable_entities + param_entities,
        'field_entities': field_entities
    }
    # === 阶段 4：函数调用关系（并行版） ===
    print(f"\n" + "="*60)
    print("阶段 4：提取 CALLS 关系（并行）...")
    """
    calls_rels = parallel_extract_with_threads(
        c_files, shared_data, process_calls_worker,
        "阶段 4：CALLS", 8
    )
    all_relations.extend(calls_rels)
    """
    for source_path in tqdm(c_files, desc="阶段 4：提取 CALLS"):
        abs_source_path = os.path.abspath(source_path)

        if len(all_relations) % 1000 == 0:
            gc.collect()

        with open(abs_source_path, 'rb') as f:
            code_bytes = f.read()
        tree = parser.parse(code_bytes)
        root = tree.root_node

        rels = extract_calls_relations(
            root, code_bytes, function_id_map, {**variable_id_map, **param_id_map}, field_id_map,
            source_path, file_visibility, entity_file_map, all_extern_functions, macro_lookup_map, source_path, all_entities, flag=True
        )
        all_relations.extend(rels)

        del tree
        del root

    # === 阶段 5：赋值关系 ===
    print(f"\n" + "="*60)
    print("阶段 5：提取 ASSIGNED_TO 关系...")
    
    for source_path in tqdm(c_files, desc="阶段 5：提取 ASSIGNED_TO"):
        abs_source_path = os.path.abspath(source_path)
        
        if len(all_relations) % 1000 == 0:
            gc.collect()

        with open(abs_source_path, 'rb') as f:
            code_bytes = f.read()
        tree = parser.parse(code_bytes)
        root = tree.root_node

        rels = extract_assigned_to_relations(
            root, code_bytes, function_id_map, {**variable_id_map, **param_id_map}, field_id_map,
            source_path, file_visibility, entity_file_map, all_extern_functions, macro_lookup_map, source_path
        )
        all_relations.extend(rels)

        del tree
        del root
    
    # === 阶段 6：语义关系 ===
    print(f"\n" + "="*60)
    print("阶段 6：提取 RETURNS / TYPE_OF...")
    
    for source_path in tqdm(c_files, desc="阶段 6：提取 RETURNS / TYPE_OF"):
        abs_source_path = os.path.abspath(source_path)

        if len(all_relations) % 1000 == 0:
            gc.collect()

        with open(abs_source_path, 'rb') as f:
            code_bytes = f.read()
        tree = parser.parse(code_bytes)
        root = tree.root_node
        # RETURNS
        rels = extract_returns_relations(
            root, code_bytes, function_id_map, {**variable_id_map, **param_id_map}, field_id_map,
            source_path, file_visibility, entity_file_map
        )
        all_relations.extend(rels)

        # TYPE_OF
        rels = extract_typeof_relations(
            root, code_bytes, variable_entities + param_entities, field_entities, struct_id_map,
            source_path, file_visibility, entity_file_map
        )
        all_relations.extend(rels)

        del tree
        del root

    # 清理内存
    del file_trees
    del file_to_entities

    # === 最终去重和统计 ===
    print(f"\n" + "="*60)
    print("去重关系...")
    original_count = len(all_relations)
    all_relations = deduplicate_relations(all_relations)
    deduplicated_count = len(all_relations)
    print(f"✅ 去重完成：{original_count} -> {deduplicated_count} (移除 {original_count - deduplicated_count} 个重复)")

    # === 输出 JSON ===
    with open(entity_path, 'w') as f:
        json.dump(all_entities, f, indent=2)
    with open(relation_path, 'w') as f:
        json.dump(all_relations, f, indent=2)

    print(f"\n✅ 提取完成：实体 {len(all_entities)} 个，关系 {len(all_relations)} 条。")
    
    # 关系统计
    relation_types = Counter([r['type'] for r in all_relations])
    print(f"\n关系类型统计：")
    for k, v in relation_types.items():
        print(f"  - {k}: {v}")
    
    # 可见性检查统计
    visibility_checked = sum(1 for r in all_relations if r.get('visibility_checked'))
    print(f"\n可见性检查覆盖：{visibility_checked}/{len(all_relations)} ({visibility_checked/len(all_relations)*100:.1f}%)")

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=str, default=r'E:\cpppro\clang_kg\linux', help="C 源码目录路径")
    parser.add_argument("--output", type=str, default=r'E:\cpppro\clang_kg\test\code_kg_with_tree-sitter\output\linux', help="输出目录路径")
    args = parser.parse_args()

    tracemalloc.start()
    start_time = time.time()
    extract_all(args.source, args.output)
    current, peak = tracemalloc.get_traced_memory()
    end_time = time.time()
    print(f"\n总耗时：{end_time - start_time:.2f} 秒")
    print(f"当前内存：{current / 1024 / 1024:.2f} MB；峰值：{peak / 1024 / 1024:.2f} MB")
    tracemalloc.stop()