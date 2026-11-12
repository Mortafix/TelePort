from os import getenv, mkdir, path
from shutil import rmtree

from dotenv import load_dotenv
from utils.report import build_report_from_json

load_dotenv()


def get_json_report(filepath):
    chat_name = path.splitext(path.basename(filepath))[0]
    report_folder = path.join(getenv("SCRIPT_FOLDER"), "reports")
    dump_file = path.join(report_folder, f"{chat_name}.xz")
    if path.exists(dump_file):
        return build_report_from_json(dump_file)


def save_json_report(report):
    report_folder = path.join(getenv("SCRIPT_FOLDER"), "reports")
    if not path.exists(report_folder):
        mkdir(report_folder)
    chat_name = path.splitext(path.basename(report.filepath))[0]
    dump_file = path.join(report_folder, f"{chat_name}.xz")
    return report.to_json(dump_file)


def remove_all_json_report():
    report_folder = path.join(getenv("SCRIPT_FOLDER"), "reports")
    if path.exists(report_folder):
        rmtree(report_folder)
