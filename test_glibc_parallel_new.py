# 在文件顶部（你已有的 imports 之后）加入：
from concurrent.futures import ProcessPoolExecutor, as_completed
import multiprocessing
import uuid
import json
import sys
import os
import re
import subprocess
import tempfile
from tqdm import tqdm
from shutil import which
import clang
from clang.cindex import Index, CursorKind, TypeKind, CompilationDatabase, TranslationUnit
from multiprocessing import Pool, cpu_count
import functools

temp_dir = '/home/lyk/work/test_pro/linux_temp'
glibc_path = '/home/lyk/work/linux'
macro_dict = {}

clang.cindex.Config.set_library_path('/usr/lib/llvm-14/lib')
index = None

def process_macro_value(value):
    """处理单个宏值列表"""
    if len(value) <= 1:
        return value
        
    to_remove = set()
    
    for i in range(len(value)):
        if i in to_remove:
            continue
            
        _, st_line1, st_column1, en_line1, en_column1 = value[i]
        info1_start, info1_end = (st_line1, st_column1), (en_line1, en_column1)
        
        for j in range(i + 1, len(value)):
            if j in to_remove:
                continue
                
            _, st_line2, st_column2, en_line2, en_column2 = value[j]
            info2_start, info2_end = (st_line2, st_column2), (en_line2, en_column2)
            
            if info1_start <= info2_start and info2_end <= info1_end:
                to_remove.add(j)
            elif info2_start <= info1_start and info1_end <= info2_end:
                to_remove.add(i)
                break
    
    # 返回处理后的结果
    return [item for idx, item in enumerate(value) if idx not in to_remove]

def process_macro_value_wrapper(args):
    """包装函数，用于多进程"""
    key, value = args
    return (key, process_macro_value(value))

def find_available_filename(original_path):
    """
    检查文件路径，如果存在就在文件名后加0
    例如: /path/to/aa.c → /path/to/aa0.c → /path/to/aa00.c
    """
    if not os.path.exists(original_path):
        return original_path
    
    # 分离路径和文件名
    directory = os.path.dirname(original_path)
    filename = os.path.basename(original_path)
    
    # 分离文件名和扩展名
    name, ext = os.path.splitext(filename)
    
    counter = 0
    while True:
        # 构建新文件名
        new_filename = f"{name}{'0' * counter}{ext}"
        new_path = os.path.join(directory, new_filename)
        
        if not os.path.exists(new_path):
            return new_path
        
        counter += 1

def post_prei(pre_i):
    with open(pre_i, "r", encoding="utf-8") as f:
        lines = f.readlines()
    st_flag = [',', ')', ']', ';']
    new_lines = []
    tmp_linum = int(lines[0].split('\n')[0].split('\t')[0].split(':')[1])
    tmp_finame = lines[0].split('\n')[0].split('\t')[0].split(':')[0]
    tmp_licon = lines[0].split('\n')[0].split('\t')[1]
    for value in lines[1:]:
        linum = int(value.split('\n')[0].split('\t')[0].split(':')[1])
        finame = value.split('\n')[0].split('\t')[0].split(':')[0]
        licon = value.split('\n')[0].split('\t')[1]

        if finame == tmp_finame and linum == tmp_linum:
            flag = 0
            for vl in st_flag:
                if licon.strip().startswith(vl):
                    flag = 1
                    break
            if flag == 1:
                tmp_licon += licon.strip()
            else:
                tmp_licon += ' ' + licon.strip()
        else:
            full_lines = f'{tmp_finame}:{tmp_linum}\t{tmp_licon}\n'
            new_lines.append(full_lines)

            tmp_linum = linum
            tmp_finame = finame
            tmp_licon = licon

    # 覆盖写回
    with open(pre_i, "w", encoding="utf-8") as f:
        f.writelines(new_lines)

def pre_process(make_arg, file_dir):
    global glibc_path
    joined_string = ' '.join(make_arg)
    # result_string = joined_string.replace('-c', '').replace('-fgnu89-inline', '').replace('-ftls-model=initial-exec', '').replace('..', glibc_path).replace('-I.', '-I' + file_dir)
    result_string = joined_string.replace('-c', '').replace('-fgnu89-inline', '').replace('-ftls-model=initial-exec', '')
    result_string = re.sub(r'\s+-o\s+.*', '', result_string)
    res_arg = result_string.split()
    res_arg[0] = '-fsyntax-only'
    return res_arg

def pre_process_args(args):
    """
    :param args: 原始编译参数列表
    :param original_file_path: entry['file'] 的绝对路径（如 '/home/lyk/work/test_pro/test_1.c'）
    :param temp_dir: 临时目录（如 './temp_dir'）
    :return: 改造后的参数列表
    """
    new_args = []
    skip_next = False
    original_file_path = args['file']
    run_args = args['arguments']
    for arg in run_args:
        if skip_next:
            skip_next = False
            continue
        if arg == '-c':
            continue  # 移除 -c
        if arg == '-o':
            skip_next = True
            continue  # 跳过原输出文件
        # arg = arg.replace('..', glibc_path).replace('-I.', '-I' + args['directory'])
        new_args.append(arg)
    
    # 强制添加 -E 选项
    if '-E' not in new_args:
        new_args.insert(1, '-E')
    new_args = new_args[:-1]
    # 生成输出文件名（test_1.c → test_1.i）
    output_filename = os.path.splitext(os.path.basename(original_file_path))[0] + '.i'
    output_path = os.path.join(temp_dir, output_filename)
    
    # 添加输入文件和输出路径（均用绝对路径）
    new_args.extend([
        original_file_path,  # 直接使用绝对路径
        '-o', output_path,
    ])
    
    return new_args

def load_command_line(json_path):
    with open(json_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    new_data = []
    for value in data:
        if value["file"].endswith('.c'):
            new_data.append(value)
    return new_data

def run_preprocess(cmd, run_path=glibc_path):
    # print("Running:", " ".join(cmd))
    result = subprocess.run(
        cmd,
        cwd=run_path,
        check=True,       # 如果返回码非零则抛出异常
        text=True,
        capture_output=True
    )

linemarker_re = re.compile(r'^#\s*([0-9]+)\s+"([^"]+)"')

def annotate_i(i_path, annotated_path):
    current_file = None
    current_line = None
    pattern = r'^(//.*|/\*.*\*/)$'
    with open(i_path, 'r', encoding='utf-8', errors='ignore') as fin, \
         open(annotated_path, 'w', encoding='utf-8') as fout:
        # annotate_stream(fin, fout)
        lines = []
        for raw_line in fin:
            line = raw_line.rstrip('\n')

            # 匹配 preprocessor line marker:  # 123 "file" flags...
            m = re.match(r'^#\s+(\d+)\s+"([^"]+)"(?:\s+(.+))?', line)
            if m:
                current_line = int(m.group(1))
                current_file = m.group(2)
                flags = m.group(3) or ""
                if flags:
                    flags = flags.strip().split(" ")[0]
                """
                if os.path.isfile(current_file):
                    with open(current_file, 'r', encoding='utf-8', errors='replace') as f:
                        lines = f.read().splitlines()
                    while current_line <= len(lines) and (lines[current_line-1].strip() == "" or lines[current_line-1].strip().startswith('#') or re.match(pattern,lines[current_line-1].strip())):
                        current_line += 1
                """
                continue  # 不输出这一行

            if current_file and current_line is not None:
                if len(line) == 0:
                    current_line += 1
                    continue
                fout.write(f"{current_file}:{current_line}\t{line}\n")
                current_line += 1
                """
                if os.path.isfile(current_file):
                    while current_line <= len(lines) and (lines[current_line-1].strip() == "" or lines[current_line-1].strip().startswith('#') or re.match(pattern,lines[current_line-1].strip())):
                        current_line += 1
                """
            else:
                # 无法关联到源文件的行，可按需求决定是否输出
                fout.write(f"???:??\t{line}\n")

def get_tokens_from_source_line(st_line, en_line, start_col, end_col, mid_lines):
    """
    start_col,end_col are 1-based column indices as in usual compilers.
    返回 (token_before, token_after, macro_text)
    token_before 或 token_after 可能为 None
    """
    if st_line == en_line:
        # Python 切片用 0-based
        sidx = start_col - 1
        eidx = end_col - 1 # end_col 是结束列的 index，假定包含最后字符（如你提供的）
        left = st_line[:sidx]
        mid = st_line[sidx:eidx]
        right = st_line[eidx:]

        # 找前一个非空 token（按连续非空白字符串拆分）
        left_tokens = re.findall(r'\S+', left)
        token_before = left_tokens[-1] if left_tokens else None

        right_tokens = re.findall(r'\S+', right)
        token_after = right_tokens[0] if right_tokens else None

        return token_before, token_after, mid
    else:
        sidx = start_col - 1
        eidx = end_col - 1 # end_col 是结束列的 index，假定包含最后字符（如你提供的）
        left = st_line[:sidx]

        right = en_line[eidx:]
        
        left_after = st_line[sidx:]
        right_before = en_line[:eidx]
        mid = left_after + ''.join(mid_lines[1:-1]) + right_before

        # 找前一个非空 token（按连续非空白字符串拆分）
        left_tokens = re.findall(r'\S+', left)
        token_before = left_tokens[-1] if left_tokens else None

        right_tokens = re.findall(r'\S+', right)
        token_after = right_tokens[0] if right_tokens else None

        return token_before, token_after, mid

def is_word_token(tok):
    # 简单判断是否为字母数字下划线组成的 token（便于用边界 \b 匹配）
    return re.fullmatch(r'[A-Za-z_]\w*', tok) is not None

def find_annotated_line_index(annotated_lines, src_file, start_line):
    """
    在 annotated_lines 中查找对应 src_file:start_line 的首个行的索引。
    可以匹配文件名的尾部（例如 annotated 使用绝对路径，但用户传相对名）。
    返回 index 或 None
    """
    base = os.path.basename(src_file)
    for i, l in enumerate(annotated_lines):
        # 格式 filename:lineno \t content
        if '\t' not in l:
            continue
        left, _ = l.split('\t', 1)
        if ':' not in left:
            continue
        fname, lineno_s = left.rsplit(':', 1)
        try:
            lineno = int(lineno_s)
        except:
            continue
        if lineno == start_line and (fname == src_file or os.path.basename(fname) == base):
            indices = [i]
            j = i + 1
            while j < len(annotated_lines):
                lj = annotated_lines[j]
                if '\t' not in lj:
                    break
                left2, _ = lj.split('\t', 1)
                if ':' not in left2:
                    break
                fname2, lineno_s2 = left2.rsplit(':', 1)
                try:
                    lineno2 = int(lineno_s2)
                except:
                    break
                if lineno2 == start_line and (fname2 == fname or os.path.basename(fname2) == base):
                    indices.append(j)
                    j += 1
                else:
                    break
            if len(indices) == 1:
                return indices[0]
            else:
                return indices
    return None

def token_in_line(line_content, token):
    if token is None:
        return False
    if is_word_token(token):
        return re.search(r'\b' + re.escape(token) + r'\b', line_content) is not None
    else:
        return token in line_content

def extract_expansion_from_annotated(annotated_path, src_file, full_content, st_num, en_num, token_before, token_after, start_col, end_col):
    """
    在 annotated.i 中找到 src_file:start_line 所在行，
    在该行中从前往后找到 token_before 的第一个匹配（取其结束位置）；
    在该行中从后往前找到 token_after 的第一个匹配（取其开始位置）；
    两者之间就是宏展开内容（strip 后返回）。
    如果对应 token 在该行找不到，会抛出 RuntimeError（按你的要求严格限定在该行）。
    返回: (extracted_text, line_content, before_span, after_span)
    before_span/after_span 是 (start,end) 字符索引或 None。
    """
    import re, os
    start_line = st_num
    end_line = en_num
    label = start_line == end_line

    def find_first_span(content, token):
        if token is None:
            return None
        if is_word_token(token):
            pat = re.compile(r'\b' + re.escape(token) + r'\b')
        else:
            pat = re.compile(re.escape(token))
        m = pat.search(content)
        return (m.start(), m.end()) if m else None

    def find_last_span(content, token):
        if token is None:
            return None
        if is_word_token(token):
            pat = re.compile(r'\b' + re.escape(token) + r'\b')
        else:
            pat = re.compile(re.escape(token))

        matches = list(pat.finditer(content))
        if not matches:
            return None
        m = matches[-1]
        return (m.start(), m.end())

    with open(annotated_path, 'r', encoding='utf-8') as f:
        annotated_lines = [ln.rstrip('\n') for ln in f]

    if label:
        idx = find_annotated_line_index(annotated_lines, src_file, start_line)
        if idx is None:
            return '', '', '', '', ''
            # raise RuntimeError(f"在 {annotated_path} 中未能找到 {src_file}:{start_line} 对应行。")
        if isinstance(idx, list):
            # 取该行内容（去掉 filename:lineno\t 前缀）
            st_parts = annotated_lines[idx[0]].split('\t', 1)
            st_content = st_parts[1] if len(st_parts) > 1 else ""

            en_parts = annotated_lines[idx[-1]].split('\t', 1)
            en_content = en_parts[1] if len(en_parts) > 1 else ""

            # 在该行里找前锚点（从前往后第一个匹配）
            before_span = find_first_span(st_content, token_before)
            if token_before is not None and before_span is None:
                return '', '', '', '', ''
            # 在该行里找后锚点（从后往前第一个匹配）
            after_span = find_last_span(en_content, token_after)
            if token_after is not None and after_span is None:
                return '', '', '', '', ''

            # 计算提取区间：从 before_span.end 到 after_span.start
            start_char = before_span[1] if before_span is not None else 0
            end_char = after_span[0] if after_span is not None else len(en_content)

            st_char = st_content[start_char:]
            en_char = en_content[:-end_char]

            macro_content = []
            for i in idx:
                fu_parts = annotated_lines[i].split('\t', 1)
                fu_content = fu_parts[1] if len(fu_parts) > 1 else ""
                if fu_content not in macro_content:
                    macro_content.append(fu_content)

            mid_extracted = ' '.join(macro_content[1:-1]).strip()
            extracted = (st_char + ' ' + mid_extracted + ' ' + en_char).strip()
            extracted_lines = ' '.join(macro_content).strip()
            macro_str = ' '.join(macro_content)

            return extracted, macro_str, before_span, after_span, extracted_lines

        else:
            # 取该行内容（去掉 filename:lineno\t 前缀）
            parts = annotated_lines[idx].split('\t', 1)
            content = parts[1] if len(parts) > 1 else ""

            # 在该行里找前锚点（从前往后第一个匹配）
            before_span = find_first_span(content, token_before)
            if token_before is not None and before_span is None:
                return '', '', '', '', ''

            # 在该行里找后锚点（从后往前第一个匹配）
            after_span = find_last_span(content, token_after)
            if token_after is not None and after_span is None:
                return '', '', '', '', ''

            # 计算提取区间：从 before_span.end 到 after_span.start
            start_char = before_span[1] if before_span is not None else 0
            end_char = after_span[0] if after_span is not None else len(content)

            if start_char > end_char:
                # 这表示匹配出现重叠或前后锚点顺序不对
                return '', '', '', '', ''

            extracted = content[start_char:end_char].strip()
            extracted_lines = content.strip()
            return extracted, content, before_span, after_span, extracted_lines
    else:
        # return '', '', '', '', ''
        st_idx = find_annotated_line_index(annotated_lines, src_file, start_line)
        en_idx = find_annotated_line_index(annotated_lines, src_file, end_line)
        if st_idx is None or en_idx is None:
            return '', '', '', '', ''
            # raise RuntimeError(f"在 {annotated_path} 中未能找到 {src_file}:{start_line} 对应行。")

        # 取该行内容（去掉 filename:lineno\t 前缀）
        st_parts = annotated_lines[st_idx].split('\t', 1)
        st_content = st_parts[1] if len(st_parts) > 1 else ""

        en_parts = annotated_lines[en_idx].split('\t', 1)
        en_content = en_parts[1] if len(en_parts) > 1 else ""
        # 在该行里找前锚点（从前往后第一个匹配）
        before_span = find_first_span(st_content, token_before)
        if token_before is not None and before_span is None:
            return '', '', '', '', ''
            # raise RuntimeError(f"在 {src_file}:{start_line} 的这一行未能找到前锚点 token_before={token_before!r}。")

        # 在该行里找后锚点（从后往前第一个匹配）
        after_span = find_last_span(en_content, token_after)
        if token_after is not None and after_span is None:
            return '', '', '', '', ''
            # raise RuntimeError(f"在 {src_file}:{end_line} 的这一行未能找到后锚点 token_after={token_after!r}。")

        # 计算提取区间：从 before_span.end 到 after_span.start
        start_char = before_span[1] if before_span is not None else 0
        end_char = after_span[0] if after_span is not None else len(en_content)

        st_char = st_content[start_char:]
        en_char = en_content[:-end_char]

        macro_content = []
        for i in range(st_idx, en_idx+1):
            fu_parts = annotated_lines[i].split('\t', 1)
            fu_content = fu_parts[1] if len(fu_parts) > 1 else ""
            macro_content.append(fu_content)

        mid_extracted = ' '.join(macro_content[1:-1]).strip()
        extracted = (st_char + ' ' + mid_extracted + ' ' + en_char).strip()
        extracted_lines = ' '.join(macro_content).strip()
        macro_str = ' '.join(macro_content)
        return extracted, macro_str, before_span, after_span, extracted_lines

# ---------- Worker: 用于第一阶段（collect entities） ----------
def collect_entities_worker(args_entry):
    """
    在子进程中运行 libclang 解析，返回 (filepath, list_of_macro_entries)
    每个 entry 格式: [file_name, start_line, start_col, end_line, end_col]
    """
    try:
        # 为子进程设置 clang path（如果需要的话）
        try:
            clang.cindex.Config.set_library_path('/usr/lib/llvm-14/lib')
        except Exception:
            pass

        idx = Index.create()
        clang_args = pre_process(args_entry['arguments'], args_entry['directory'])
        # 解析 translation unit
        tu = idx.parse(args_entry['file'], args=clang_args,
                       options=TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD)
        local_macros = []

        def visitor(cursor):
            # 仅收集发生在文件本身的宏实例
            if cursor.kind == CursorKind.MACRO_INSTANTIATION and cursor.location and cursor.location.file and cursor.location.file.name == args_entry['file']:
                file_name = cursor.location.file.name
                st_line = cursor.extent.start.line
                en_line = cursor.extent.end.line
                st_column = cursor.extent.start.column
                en_column = cursor.extent.end.column
                addmacro = [file_name, st_line, st_column, en_line, en_column]
                # 去重（local list 里）
                if addmacro not in local_macros:
                    local_macros.append(addmacro)
            for c in cursor.get_children():
                visitor(c)

        visitor(tu.cursor)
        return (args_entry['file'], local_macros, None)
    except Exception as e:
        return (args_entry.get('file', None), [], f"ERROR: {e}")

# ---------- Worker: 用于第二阶段（处理单个文件的预处理 + 抽出） ----------
def process_file_worker(task):
    """
    task: (args_entry, macro_list_for_this_file, temp_dir_base)
    在临时目录中生成 .i/.anno 并在退出时自动删除（TemporaryDirectory）
    返回: (file, list_of_save_dicts, None, None, maybe_error)
    """
    args_entry, macro_infile, temp_dir_base = task
    try:
        # 每个 worker 使用独立短生命周期临时目录
        with tempfile.TemporaryDirectory(prefix="preproc_") as td:
            bsname = os.path.basename(args_entry['file'])
            # 生成 annotated 路径 & .i 输出都放到 td
            annotated_i = os.path.join(td, f"{bsname}.{uuid.uuid4().hex}.anno")

            # 构建预处理命令（先用现有 pre_process_args 得到 cmd，然后把 -o -> td 下文件替换）
            cmd = pre_process_args(args_entry)
            # pre_process_args 通常在尾部包含 '-o', '/abs/path/name.i'
            # 我们要保证输出写到 td 下，避免原来位置出现大量 .i
            # 找到最后一个以 .i 结尾的参数并替换为 td/xxx.i
            outi = cmd[-1]
            cmd[-1] = os.path.join(td, os.path.basename(outi))
            # 现在 pre_i 指向 td 下的 .i 文件
            pre_i = cmd[-1]

            # 运行预处理（会在 args_entry["directory"] 下执行，输出 .i 到 td）
            run_preprocess(cmd, args_entry["directory"])

            # annotate i -> annotated_i（annotate_i 读取 pre_i 写 annotated_i）
            annotate_i(pre_i, annotated_i)
            post_prei(annotated_i)

            save_macro_list_local = []
            macro_infile = sorted(macro_infile, key=lambda x: (x[1], x[3]))

            with open(args_entry['file'], 'r', encoding='utf-8', errors='replace') as f:
                lines = f.read().splitlines()

            for sin_macro in macro_infile:
                src_file, start_line, start_col, end_line, end_col = sin_macro
                if start_line < 1 or start_line > len(lines):
                    continue
                src_line = lines[start_line - 1]
                stp_line = lines[end_line - 1]
                mid_lines = lines[start_line - 1:end_line]
                token_before, token_after, macro_text = get_tokens_from_source_line(
                    src_line, stp_line, start_col, end_col, mid_lines
                )

                expansion, content, before_span, after_span, extracted_lines = extract_expansion_from_annotated(
                    annotated_i, src_file, lines[start_line - 1:end_line],
                    start_line, end_line, token_before, token_after, start_col, end_col
                )

                if expansion:
                    location = [start_line, start_col, end_line, end_col]
                    save_dict = {
                        "file": args_entry['file'],
                        "location": location,
                        "name": macro_text,
                        "macro": expansion,
                        "extracted_lines": extracted_lines
                    }
                    save_macro_list_local.append(save_dict)

            # 离开 with 块时 TemporaryDirectory 会自动删除 td 及其中的 .i/.anno 文件
            return (args_entry['file'], save_macro_list_local, None, None, None)

    except Exception as e:
        return (args_entry.get('file', None), [], None, None, str(e))


# ---------- 修改后的 main() ----------
def main():
    save_macro_dict = {}
    save_macro_list = []
    file_to_annotated = {}
    cmdjson = '/home/lyk/work/linux/compile_commands.json'

    all_args = load_command_line(cmdjson)

    # ---------- 第一阶段：并行解析 compilation db（收集 macro 位置信息） ----------
    macro_dict_local = {}
    errors = []
    max_workers = min((os.cpu_count() or 4), 8)
    with ProcessPoolExecutor(max_workers=max_workers) as ex:
        futures = { ex.submit(collect_entities_worker, args): args for args in all_args }
        for fut in tqdm(as_completed(futures), total=len(futures), desc="Parsing with libclang (parallel)"):
            args = futures[fut]
            try:
                filepath, local_macros, err = fut.result()
                if err:
                    errors.append((filepath, err))
                if local_macros:
                    macro_dict_local[filepath] = local_macros
            except Exception as e:
                errors.append((args.get('file', None), str(e)))
    """
    items = list(macro_dict_local.items())
    with ProcessPoolExecutor(max_workers=max_workers) as executor:
        # 提交所有任务
        futures = {executor.submit(process_macro_value_wrapper, item): item for item in items}
        
        # 使用 tqdm 显示进度
        results = []
        for future in tqdm(as_completed(futures), total=len(futures), desc="Processing macros"):
            try:
                result = future.result()
                results.append(result)
            except Exception as e:
                print(f"Error processing task: {e}")
        
        # 更新字典
        for key, new_value in results:
            macro_dict_local[key] = new_value
    """
    # ---------- 第二阶段：并行处理每个包含宏的文件（预处理 + annotate + extract） ----------
    tasks = []
    for args in all_args:
        if args['file'] in macro_dict_local:
            tasks.append( (args, macro_dict_local[args['file']], temp_dir) )

    results = []
    with ProcessPoolExecutor(max_workers=max_workers) as ex:
        futures = { ex.submit(process_file_worker, t): t for t in tasks }
        for fut in tqdm(as_completed(futures), total=len(futures), desc="Preprocess+annotate+extract (parallel)"):
            try:
                file_path, save_macro_list_local, annotated_i, pre_i, err = fut.result()
                if err:
                    print(f"[WARN] file {file_path} worker error: {err}")
                    continue
                # 合并结果到主进程的数据结构
                if save_macro_list_local:
                    save_macro_list.extend(save_macro_list_local)
                    save_macro_dict[file_path] = save_macro_list_local
                if annotated_i:
                    file_to_annotated[file_path] = annotated_i
                # optional: 主进程可写 expansion.txt/其他文件（你原脚本每次覆盖写 expansion.txt，这里我把写入保留到主进程）
                """
                for item in save_macro_list_local:
                    print("\n--- 拆出的宏展开内容（写入 expansion.txt）---\n")
                    print(item['macro'])
                    with open("expansion.txt", "w", encoding='utf-8') as out:
                        out.write(item['macro'])
                    print(f"\nannotated.i 已生成: {annotated_i}")
                    print(f"预处理输出 .i: {pre_i}")
                    print("结果也保存为 expansion.txt （当前工作目录）")
                """
            except Exception as e:
                print("Worker exception:", e)

    # ---------- 把结果写盘（与原脚本最后的几行对应） ----------
    save_path = '/home/lyk/work/test/code_kg_with_tree-sitter/output/linux/macro_list_1.json'
    with open(save_path, "w", encoding="utf-8") as f:
        json.dump(save_macro_list, f, ensure_ascii=False, indent=4)
    directory = os.path.dirname(save_path)
    new_filename = "macro_dict.json"
    new_path = os.path.join(directory, new_filename)
    with open(new_path, "w", encoding="utf-8") as f:
        json.dump(save_macro_dict, f, ensure_ascii=False, indent=4)
    src_to_an = os.path.join(directory, 'file_to_anno.json')
    with open(src_to_an, "w", encoding="utf-8") as f:
        json.dump(file_to_annotated, f, ensure_ascii=False, indent=4)

    if errors:
        print("Some errors occurred during parallel processing:")
        for e in errors:
            print(e)

# 保持 __main__ 判定
if __name__ == "__main__":
    main()