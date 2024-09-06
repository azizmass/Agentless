import argparse
import json
import os
import logging
from langchain_core.documents import Document

from langchain_community.graphs.graph_document import Node, Relationship, GraphDocument
from langsmith import traceable

from Agentless.agentless.get_repo_structure.get_repo_structure import get_project_structure_from_scratch
from Agentless.agentless.localisation.FL import LLMFL
from Agentless.agentless.util.preprocess_data import filter_none_python, filter_out_test_files
from apps.helper import read_file
from apps.services.neo4jDB.graphDB_dataAccess import create_graph_database_connection
from apps.services.open_ia_llm import OpenIA_LLM, get_graph_with_schema
from apps.services.quality_checkers.requirements_qte_check import schema_req, RequirementsQTECheck
from apps.services.code_skeleton_extractor import filtered_methods_by_file_name_function

FILES_TO_USE = [
    "interface.py",
    "catalog.py",
    "__init__.py",
]

def filter_files(structure: dict, files: list):
    files_level = structure.keys()
    files_to_remove = []
    for file in files_level:
        if file.lower().endswith(".py"):
            if file.lower() not in files:
                files_to_remove.append(file)
        if "." not in file and isinstance(structure[file], dict):
            filter_files(structure[file], files)
    for file in files_to_remove:
        del structure[file]
    return structure


def get_test_steps(req_path):
    with open("app-config.json", "r") as f:
        data = json.load(f)
        count = data["reps"]
    requirement_text = read_file(req_path)
    bad_chars = ["\n"]
    result_req = False
    req_doc = Document(page_content=''.join(i for i in requirement_text if i not in bad_chars))
    final_req_graph = {
        "nodes": [],
        "relationships": []
    }
    for i in range(count):
        graph_requirement = get_graph_with_schema([req_doc], schema_req, OpenIA_LLM.default_prompt_requirement,
                                                  req_path,
                                                  input_dict={
                                                      "input": requirement_text
                                                  })
        final_req_graph = {
            "nodes": [],
            "relationships": []
        }
        for doc in graph_requirement:
            for node in doc.nodes:
                node_pre = Node(
                    id=node.id,
                    type=node.type,
                    properties={
                        key: val for key, val in node.properties.items() if key != "embeddings"
                    }
                )
                final_req_graph["nodes"].append(node_pre)
            for relation in doc.relationships:
                relation_pre = Relationship(
                    type=relation.type,
                    source=Node(id=relation.source.id, type=relation.source.type),
                    target=Node(id=relation.target.id, type=relation.target.type),
                    properties={
                        key: val for key, val in relation.properties.items() if key != "embeddings"
                    }
                )
                final_req_graph["relationships"].append(relation_pre)

        if RequirementsQTECheck.check(final_req_graph):
            logging.error(f"Quality check validated for requirement at iteration {i}")
            result_req = True
            break
    if not result_req:
        logging.error("Quality check failed for requirement")
        return
    return final_req_graph["nodes"]


def recursive_filter_files(sequence, methode, obj_loc):
    if len(sequence) == 0:
        temp = obj_loc
        if type(obj_loc) == dict:
            if "methods" in obj_loc:
                temp["methods"] = []
                for method in obj_loc["methods"]:
                    if methode in method["method"]:
                        temp["methods"].append(method)
        elif type(obj_loc) == list:
            temp = []
            for obj in obj_loc:
                if methode in obj["method"]:
                    temp.append(obj)
        return temp
    path = sequence[0]
    if path in obj_loc:
        return recursive_filter_files(sequence[1:], methode, obj_loc[path])


def filter_taf_files(sequence, file_name, files_locs, full_obj, methods):
    if len(sequence) == 0:
        if file_name in full_obj:
            items = full_obj[file_name]
            result = []
            for item in items:
                if item["class"] is None:
                    for method in methods:
                        if method in item["method"]:
                            result.append(item)
                            break
                else:
                    result_class = []
                    class_methods = item["methods"]
                    for method in class_methods:
                        for m in methods:
                            if m in method["method"]:
                                result_class.append(method)
                                break
                    if len(result_class) > 0:
                        item["methods"] = result_class
                        result.append(item)
            files_locs[file_name] = result
        return
    path = sequence[0]
    if path in full_obj:
        if path not in files_locs:
            files_locs[path] = {}
        filter_taf_files(sequence[1:], file_name, files_locs[path], full_obj[path], methods)


def verify_sequence(sequence, locs_seq):
    if len(sequence) > len(locs_seq):
        return False
    for i, seq in enumerate(sequence):
        if seq != locs_seq[i]:
            return False
    return True


def find_locs_that_matches_files(sequence, file_name, locs):
    res = []
    for loc in locs:
        loc_seq = loc.split(":")
        if len(loc_seq) < 2:
            continue
        if loc_seq[0].strip() == "":
            continue
        if loc_seq[1].strip() == "":
            continue
        if not verify_sequence(sequence, loc_seq[0].split(".")):
            continue
        if len(sequence) == len(loc_seq[0].split(".")):
            res.append(loc_seq[1].strip())
            continue
        if len(sequence) > len(loc_seq[0].split(".")) - 1:
            continue
        index = len(sequence)
        if not loc_seq[0].split(".")[index].strip().lower().replace(".py", "") in file_name.lower().replace(".py", ""):
            continue
        res.append(loc_seq[1].strip())
    return res


@traceable
def verification_with_skeleton(locs, files, fl, graph):
    final_locs = []
    methods = []
    for loc in locs:
        loc_seq = loc.split(":")
        if len(loc_seq) < 2:
            continue
        if loc_seq[0].strip() == "":
            continue
        if loc_seq[1].strip() == "":
            continue
        methods.append(loc_seq[1].strip())

    files_locs = filtered_methods_by_file_name_function(graph, files, methods)
    skeleton = fl.give_skeleton(files_locs)
    for line in skeleton:
        seq = line['step_explication'].split(":")
        if len(seq) < 2:
            continue
        if seq[0].strip() == "":
            continue
        if seq[1].strip() == "":
            continue
        label = seq[0].strip()
        step_explication = seq[1].strip()
        locs_line = fl.verify_tools_by_line(step_explication, line['methods_used'], label, graph)
        for loc in locs_line:
            if loc not in final_locs:
                final_locs.append(loc)
    return final_locs


@traceable
def localize(args, test_steps):
    requirement = read_file(args.req_path)
    graph = create_graph_database_connection(args)
    d = get_project_structure_from_scratch(
        "MehdiMeddeb/taf_tools", None, args.instance_id, "playground"
    )

    final_output = {
        "instance_id": d["instance_id"],
        "found_files": [],
        "locs": []
    }
    nodes = []
    relationships = []
    doc_ref = args.doc_ref

    instance_id = d["instance_id"]

    logging.info(f"================ localize {instance_id} ================")

    structure = filter_files(d["structure"], FILES_TO_USE)
    filter_none_python(structure)
    # some basic filtering steps
    # filter out test files (unless its pytest)
    if not d["instance_id"].startswith("pytest"):
        filter_out_test_files(structure)
    for test_step in test_steps:
        found_related_locs = []
        fl = LLMFL(
            d["instance_id"],
            structure,
            requirement,
            test_step.properties['explanation'],
            OpenIA_LLM.version,
        )
        found_files, additional_artifact_loc_file, file_traj = fl.localize(d["instance_id"])

        # related class, functions, global var localization
        if len(found_files) != 0:
            pred_files = found_files[: args.top_n]
            fl = LLMFL(
                d["instance_id"],
                structure,
                requirement,
                test_step.properties['explanation'],
                OpenIA_LLM.version,
            )

            (
                found_related_locs,
                additional_artifact_loc_related,
                related_loc_traj,
            ) = fl.localize_function_from_compressed_files(
                pred_files,
            )

        pred_files = found_files[: args.top_n]
        fl = LLMFL(
            instance_id,
            structure,
            requirement,
            test_step.properties['explanation'],
            OpenIA_LLM.version,
        )
        coarse_found_locs = {}
        for i, pred_file in enumerate(pred_files):
            if len(found_related_locs) > i:
                coarse_found_locs[pred_file] = found_related_locs[i]
        (
            found_edit_locs,
            additional_artifact_loc_edit_location,
            edit_loc_traj,
        ) = fl.localize_line_from_coarse_function_locs(
            pred_files,
            coarse_found_locs,
            context_window=args.context_window,
            sticky_scroll=args.sticky_scroll,
            temperature=args.temperature,
        )

        locs_to_return = set()
        locs_found = found_related_locs + found_edit_locs
        for loc in locs_found:
            if (loc[0]) and (loc[0] != ""):
                texts = loc[0].split("\n")
                for text in texts:
                    locs_to_return.add(text)

        for file in found_files:
            if file not in final_output["found_files"]:
                final_output["found_files"].append(file)

        locs_to_return = verification_with_skeleton(locs_to_return, found_files, fl, graph)

        for loc in locs_to_return:
            if loc not in final_output["locs"]:
                final_output["locs"].append(loc)
            segment = loc.split(":")
            node = Node(
                id=f"{instance_id}--||--{test_step.id}--||--{loc}",
                type="Tool_Suggestion",
                properties={
                    "doc_ref": doc_ref,
                    "function": loc.split(":")[1].strip() if len(segment) > 1 else "",
                    "path": loc.split(":")[0].strip() if len(segment) > 1 else loc,
                }
            )
            relationship = Relationship(
                source=Node(id=test_step.id, type="Test_step"),
                target=node,
                type="HAS_TOOL_SUGGESTION"
            )
            nodes.append(node)
            relationships.append(relationship)

    if not os.path.exists(args.output_file):
        data = [
            final_output
        ]
    else:
        with open(args.output_file, "r") as f:
            data = json.load(f)
            data = [p for p in data if p['instance_id'] != d["instance_id"]]
            data.append(final_output)
    with open(args.output_file, "w") as f:
        f.write(
            json.dumps(data)
        )
    return GraphDocument(
        nodes=nodes,
        relationships=relationships,
        source=Document(page_content=requirement)
    ), final_output


def main():
    def parse_args():
        parser = argparse.ArgumentParser()
        parser.add_argument('-dBuserName', type=str, default=os.environ['NEO4J_USERNAME'], help='database userName')
        parser.add_argument('-gituserName', type=str, default='Mehdi', help='git userName')
        parser.add_argument('-cmd', type=str, default='FULL', help='cmd')
        parser.add_argument('-database', type=str, default='neo4j', help='database name')
        parser.add_argument('-req_path', type=str,
                            default="datasets/datasets/requirements/sensing_powerpath_current.txt",
                            help='requirement file path')
        parser.add_argument('-instance_id', type=str, default="sensing_powerpath_current", help='instance id')
        parser.add_argument('-output_folder', type=str, default="tests", help='output folder')
        parser.add_argument('-output_file', type=str, default="loc_outputs.jsonl", help='output file')
        parser.add_argument('-doc_ref', type=str,
                            default="datasets/datasets/requirements/sensing_powerpath_current.txt",
                            help='doc ref')
        parser.add_argument('-top_n', type=int, default=25, help='top n')
        parser.add_argument('-temperature', type=float, default=0.0, help='temperature')
        parser.add_argument('-sticky_scroll', type=bool, default=False, help='sticky scroll')
        parser.add_argument('-context_window', type=int, default=20, help='context window')
        return parser.parse_args()

    args = parse_args()

    # list files in the folder datasets/val-localization/requirements
    requirements = os.listdir(
        "datasets/requirements"
    )

    args.output_file = os.path.join(str(args.output_folder), str(args.output_file))

    os.makedirs(args.output_folder, exist_ok=True)

    for requirement in requirements:
        args.req_path = os.path.join(
            "datasets/requirements", requirement)
        args.instance_id = requirement.replace(".txt", "")
        test_steps = get_test_steps(args.req_path)
        localize(args, test_steps)

@traceable
def localization_update_path(args, doc_ref, test_steps):
    path_req = doc_ref.split("--||--")[0]
    args.req_path = path_req
    args.instance_id = path_req.split("/")[-1].replace(".txt", "")
    args.doc_ref = doc_ref
    return localize(args, test_steps)


if __name__ == "__main__":
    main()