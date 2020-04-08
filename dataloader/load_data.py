import os
import pandas
import json
import logging
import multiprocessing
import time
import random
from linetimer import CodeTimer
from Configs import getConfig
from py2neo import Graph
from DZDjson2GraphIO import Json2graphio


config = getConfig()
log = logging.getLogger(__name__)
log.addHandler(logging.StreamHandler())
log.setLevel(getattr(logging, config.LOG_LEVEL))
graph = Graph(config.NEO4J_CON)


class FullTextPaperJsonFilesIndex(object):
    _index = None

    def __init__(self, base_path):
        self._index = {}
        self.base_path = base_path
        self._index_dirs(self.base_path)

    def _index_dirs(self, path):
        for root, dirs, files in os.walk(path):
            for file in files:
                # Each full text paper in the CORD-19 dataset comes as a file in json format.
                # The filename is made of the paper id (a sha hash of the origin pdf)
                file_id = os.path.splitext(os.path.basename(file))[0]
                self._index[file_id] = os.path.join(root, file)

    def get_full_text_paper_path(self, paper_sha):
        if paper_sha is None:
            return None
        try:
            return self._index[paper_sha]
        except KeyError:
            return None


json_files_index = FullTextPaperJsonFilesIndex(config.DATA_BASE_DIR)
# ToDo:
# * (postponed for now)Create a option to bootstrap a paper only by json, for supplement papers. hang these supplemental papers on the main paper


class Paper(object):
    _raw_data_json = None
    _raw_data_csv_row = None
    properties = None

    # child objects
    Author = None
    PaperID = None
    Reference = None
    BodyText = None
    Abstract = None

    def __init__(self, row: pandas.Series):
        # cord_uid,sha,source,title,doi,pmcid,pubmed_id,license,abstract,publish_time,author,journal,microsoft_academic_id,who_covidence,has_full_text,full_text_file,url
        self._raw_data_csv_row = row
        # some row refernce multiple json files (in form of a sha hash of the paper).
        # in most cases the files are the same (dunno why)
        # in some cases the extra papers a supplemental papers, in some cases they are reviewed version.
        # at the moment we ignore all this and only take the last refernce in list, as it usally the most recent paper and not a supplemental paper (not always :/ )
        # ToDo: Distinguish duplicate, supplemental and reviewd papers. Ignore duplicates and store the supplemental paper somehow
        if not pandas.isna(row["sha"]):
            full_text_paper_id = [pid.strip() for pid in row["sha"].split(";")][-1:]
            self.paper_sha = full_text_paper_id[0] if full_text_paper_id[0] else None
            self._load_full_json()
        else:
            self.paper_sha = None

        self.properties = {"cord19-id": self.paper_sha}
        self.PaperID = []
        self.Reference = []
        self.BodyText = []
        self.Abstract = []
        PaperParser(self)

    def to_dict(self):
        dic = self.properties
        # sub/child dicts
        dic["Author"] = self.Author
        dic["PaperID"] = self.PaperID
        dic["Reference"] = self.Reference
        dic["BodyText"] = self.BodyText
        dic["Abstract"] = self.Abstract
        return dic

    def _load_full_json(self):
        self.full_text_source_file_path = None
        self.full_text_source_file_path = json_files_index.get_full_text_paper_path(
            self.paper_sha
        )
        #####################################

        if self.full_text_source_file_path is not None:
            json_data = None
            with open(self.full_text_source_file_path) as json_file:
                json_data = json.load(json_file)
            self._raw_data_json = json_data


class PaperParser(object):
    def __init__(self, paper: Paper):

        self.paper = paper
        self.parse_paper_properties()
        self.parse_paper_ids()
        self.parse_authors()
        self.parse_references()
        self.parse_abstract()
        self.parse_body_text()

    def parse_paper_properties(self):
        for prop_name in config.METADATA_PAPER_PROPERTY_COLUMNS:
            prop_val = self.paper._raw_data_csv_row[prop_name]
            if not pandas.isna(prop_val):
                self.paper.properties[prop_name] = prop_val

    def parse_authors(self):
        def parse_author_row(paper_row):
            authors_cell = paper_row["author"]
            authors = []
            if pandas.isna(authors_cell):
                return authors
            for author_str in authors_cell.split(";"):
                author = {"last": None, "first": None, "middle": None}
                try:
                    author["last"], author["first"] = author_str.split(",")
                except ValueError:
                    last = author_str
                if author["first"] is not None:
                    try:
                        author["fist"], author["middle"] = author["first"].split(" ")
                    except ValueError:
                        pass
                authors.append(author)
            return authors

        # First we check if there is json data of authors, which is more detailed and allready formated(+pre splitted,+affiliation,+location)
        try:
            self.paper.Author = self.paper._raw_data_json["metadata"]["authors"]
        except (KeyError, TypeError):
            # if not we will parse the author string in the metadata.csv row
            self.paper.Author = parse_author_row(self.paper._raw_data_csv_row)

    def parse_paper_ids(self):
        for id_col in config.METADATA_FILE_ID_COLUMNS:
            paper_id_name = self._normalize_paper_id_name(id_col)
            paper_id = self.paper._raw_data_csv_row[id_col]
            if not pandas.isna(paper_id):
                self.paper.PaperID.append({"type": paper_id_name, "id": str(paper_id)})

    def parse_references(self):
        refs = []
        try:
            raw_refs = self.paper._raw_data_json["bib_entries"]
        except (KeyError, TypeError):
            return refs
        for ref_name, raw_attrs in raw_refs.items():
            ref = {"name": ref_name}
            for ref_attr_name, ref_attr_val in raw_attrs.items():
                # Save simple attributes
                if (
                    ref_attr_name in config.FULLTEXT_PAPER_BIBREF_ATTRS
                    and isinstance(ref_attr_val, (str, int))
                    and ref_attr_val != ""
                ):
                    ref[ref_attr_name] = ref_attr_val
                # save public IDs
                ref["PaperID"] = []
                if ref_attr_name == "other_ids":
                    for id_type, id_vals in ref_attr_val.items():
                        paper_id_name = self._normalize_paper_id_name(id_type)
                        for id_val in id_vals:

                            ref["PaperID"].append({"type": paper_id_name, "id": id_val})
                refs.append(ref)
        self.paper.Reference = refs

    def parse_body_text(self):
        body_texts = []
        if self.paper._raw_data_json is not None:
            for body_text in self.paper._raw_data_json["body_text"]:
                if "cite_spans" in body_text:
                    self._link_references(body_text["cite_spans"])
                # delete non needed data
                if "eq_spans" in body_text:
                    del body_text["eq_spans"]
                if "ref_spans" in body_text:
                    del body_text["ref_spans"]
                self.paper.BodyText.append(body_text)

    def parse_abstract(self):
        abstract_sections = []
        if self.paper._raw_data_json is not None:
            for abstract_sections in self.paper._raw_data_json["abstract"]:
                if "cite_spans" in abstract_sections:
                    self._link_references(abstract_sections["cite_spans"])
                # delete non needed data
                if "eq_spans" in abstract_sections:
                    del abstract_sections["eq_spans"]
                if "ref_spans" in abstract_sections:
                    del abstract_sections["ref_spans"]
                self.paper.Abstract.append(abstract_sections)
        else:
            abst = self.paper._raw_data_csv_row["abstract"]
            if not pandas.isna(abst):
                self.paper.Abstract.append({"text": abst})

    def _link_references(self, ref_list):
        for ref in ref_list:
            if "ref_id" in ref:
                ref["Reference"] = self._find_reference(ref["ref_id"])
                del ref["ref_id"]

    def _normalize_paper_id_name(self, paper_id_name):
        for (
            correct_format,
            occurent_format,
        ) in config.PAPER_ID_NAME_NORMALISATION.items():
            if paper_id_name in occurent_format or paper_id_name == correct_format:
                return correct_format
        return paper_id_name

    def _find_reference(self, ref_name):
        for ref in self.paper.Reference:
            if ref_name == ref["name"]:
                return ref
        return ref_name


class Dataloader(object):
    def __init__(
        self, metadata_csv_path, from_row=None, to_row=None, worker_name: str = None,
    ):
        self.name = worker_name
        self.data = pandas.read_csv(metadata_csv_path)[from_row:to_row]

        self.data = self.data.rename(
            columns=config.METADATA_FILE_COLUMN_OVERRIDE, errors="raise"
        )
        self._build_loader()

    def parse(self):
        papers = []
        paper_total_count = len(self.data)

        paper_count = 0
        for index, row in self.data.iterrows():

            papers.append(Paper(row))
            if len(papers) == config.PAPER_BATCH_SIZE:
                log.info(
                    "{}Load next {} papers.".format(
                        self.name + ": " if self.name else "", len(papers)
                    )
                )
                self.load(papers)
                paper_count += len(papers)
                del papers
                papers = []
                log.info(
                    "{}Loaded {} from {} papers.".format(
                        self.name + ": " if self.name else "",
                        paper_count,
                        paper_total_count,
                    )
                )
        self.load(papers)

    def load(self, papers):

        for index, paper in enumerate(papers):
            self.loader.load_json("Paper", paper.to_dict())
        try:
            if db_loading_lock is not None:
                db_loading_lock.acquire()
                log.info(
                    "{}Acquired DB loading lock.".format(
                        self.name + ": " if self.name else ""
                    )
                )
        except NameError:
            # we are in singlethreaded mode. no lock set
            pass
        self.loader.create_indexes(graph)
        self.loader.merge(graph)
        try:
            if db_loading_lock is not None:
                log.info(
                    "{}Release DB loading lock.".format(
                        self.name + ": " if self.name else ""
                    )
                )
                db_loading_lock.release()
        except NameError:
            # we are in singlethreaded mode. no lock set
            pass

    def _build_loader(self):
        c = Json2graphio()
        # c.config_dict_label_override = config.JSON2GRAPH_LABELOVERRIDE
        # c.config_func_custom_relation_name_generator = DataTransformer.nameRelation
        c.config_dict_primarykey_generated_hashed_attrs_by_label = (
            config.JSON2GRAPH_GENERATED_HASH_IDS
        )
        c.config_dict_concat_list_attr = config.JSON2GRAPH_CONCAT_LIST_ATTR
        c.config_str_collection_anchor_label = config.JSON2GRAPH_COLLECTION_NODE_LABEL
        c.config_list_collection_anchor_extra_labels = (
            config.JSON2GRAPH_COLLECTION_EXTRA_LABELS
        )
        c.config_graphio_batch_size = config.COMMIT_INTERVAL
        # c.config_dict_primarykey_attr_by_label = config.JSON2GRAPH_ID_ATTR
        c.config_str_primarykey_generated_attr_name = (
            config.JSON2GRAPH_GENERATED_HASH_ID_ATTR_NAME
        )
        c.config_bool_capitalize_labels = False
        c.config_dict_label_override = config.JSON2GRAPH_LABEL_OVERRIDE
        # c.config_func_node_pre_modifier = DataTransformer.renameLabels
        # c.config_func_node_post_modifier = DataTransformer.addExtraLabels
        # c.config_dict_property_name_override = config.JSON2GRAPH_PROPOVERRIDE
        self.loader = c

    # Todo: Make Worker class to function and create pool
    # https://stackoverflow.com/questions/20886565/using-multiprocessing-process-with-a-maximum-number-of-simultaneous-processes


def worker_func(
    metadata_csv_path, from_row: int, to_row: int, worker_name: str,
):

    log.info("Start {} -- row {} to row {}".format(worker_name, from_row, to_row))
    try:
        dataloader = Dataloader(
            metadata_csv_path,
            from_row=from_row,
            to_row=to_row,
            worker_name=worker_name,
        )
        dataloader.parse()
    except Exception as er:
        print(er)
        log.exception(er)
        raise er


def worker_init(l):
    global db_loading_lock
    db_loading_lock = l


def load_data_mp(worker_count: int, rows_per_worker=None):

    row_count_total = len(pandas.read_csv(config.METADATA_FILE).dropna(how="all"))

    if rows_per_worker is None:
        # just distribute all rows to workers. all workers will run simulationsly
        rows_per_worker = int(row_count_total / worker_count)
        leftover_rows = row_count_total % worker_count
        worker_instances_count = worker_count
    else:
        # we create a queue of workers, only <worker_count> will run simulationsly
        worker_instances_count = int(row_count_total / rows_per_worker) or 1
        leftover_rows = row_count_total % rows_per_worker

    lock = multiprocessing.Lock()
    worker_queue = []

    worker_pool = multiprocessing.Pool(
        worker_count, initializer=worker_init, initargs=(lock,)
    )

    rows_distributed = 0
    for worker_index in range(0, worker_instances_count):
        from_row = rows_distributed
        rows_distributed += rows_per_worker
        if worker_index == worker_instances_count:
            # last worker gets the leftofter rows
            rows_distributed += leftover_rows
        worker_name = "WORKER_{}".format(worker_index)
        log.info("Create worker '{}'".format(worker_name))
        worker_pool.apply_async(
            worker_func,
            args=(config.METADATA_FILE, from_row, rows_distributed, worker_name),
        )
        rows_distributed += 1
    worker_pool.close()
    worker_pool.join()


# pandas.read_csv(config.METADATA_FILE)
def load_data():
    dataloader = Dataloader(config.METADATA_FILE)
    dataloader.parse()


if __name__ == "__main__":
    with CodeTimer(unit="s"):
        load_data_mp(config.NO_OF_PROCESSES, config.PAPER_BATCH_SIZE)
        # load_data_mp(1)
        # load_data()
