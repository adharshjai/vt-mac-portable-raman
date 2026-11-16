"""The path rules shared by uploads to ARC and the ARC classifier job (PROTOCOL.md 5.6, raman_datafed/arc_paths.py).

ARC's Projects collection in Globus shows the cluster folder /projects as its top "/". These tests
pin the one function that turns a cluster path into a path inside that collection, the check of the
ARC folder setting, and the two helpers for the job and the demo. Pure functions: nothing here starts
a program or connects anywhere.
"""

import os
import random
import socket
import unittest
from pathlib import Path
from unittest import mock

import support
from raman_datafed import arc_paths
from raman_datafed.arc_paths import FOLDER_PART, check_cluster_folder, collection_path, demo_local_path, relative_below

ROOT = "/projects"
DEFAULT_FOLDER = "/projects/pcl_photonics/raman_globus_upload"


def _no_network(*_arguments, **_options):
    raise AssertionError("an ARC test tried to open a network connection")


class ArcCase(unittest.TestCase):
    """Every ARC test runs with RAMAN_ARC_TEST_GUARD=1 (PROTOCOL.md 5.6) and without network."""

    def setUp(self):
        for patcher in (mock.patch.dict(os.environ, {"RAMAN_ARC_TEST_GUARD": "1"}),
                        mock.patch.object(socket.socket, "connect", _no_network),
                        mock.patch.object(socket, "create_connection", _no_network)):
            patcher.start()
            self.addCleanup(patcher.stop)


class CollectionPath(ArcCase):
    def test_the_rule_of_section_5(self):
        cases = (
            # cluster path, root, Globus path
            (DEFAULT_FOLDER, ROOT, "/pcl_photonics/raman_globus_upload"),
            ("/projects/pcl_photonics/raman_globus_upload/_incoming/s.u3f9a0c1d2e4b", ROOT,
             "/pcl_photonics/raman_globus_upload/_incoming/s.u3f9a0c1d2e4b"),
            ("/projects/pcl_photonics/raman_globus_upload/simulated", ROOT,
             "/pcl_photonics/raman_globus_upload/simulated"),
            # The root itself is the collection's top.
            (ROOT, ROOT, "/"),
            # A collection that shows the whole cluster: paths stay as they are.
            (DEFAULT_FOLDER, "/", DEFAULT_FOLDER),
            ("/", "/", "/"),
            # Another root.
            ("/work/team/x", "/work", "/team/x"),
            # A trailing slash is kept as it is; the rest of the path is not touched.
            ("/projects/pcl_photonics/x/", ROOT, "/pcl_photonics/x/"),
        )
        for cluster, root, expected in cases:
            with self.subTest(cluster=cluster, root=root):
                self.assertEqual(collection_path(cluster, root), expected)

    def test_a_path_outside_the_root_is_refused(self):
        for cluster in ("/home/someone/x", "/projectsx/pcl/x", "/project/pcl", "projects/pcl/x", "", "/"):
            with self.subTest(cluster=cluster):
                with self.assertRaises(ValueError) as raised:
                    collection_path(cluster, ROOT)
                self.assertIn("is not inside /projects", str(raised.exception))

    def test_500_generated_paths_lose_exactly_their_projects_prefix(self):
        generator = random.Random(20261115)
        alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"

        def part():
            return generator.choice(alphabet[:62]) + "".join(
                generator.choice(alphabet) for _ in range(generator.randrange(0, 20)))

        for _ in range(500):
            below = "/" + "/".join(part() for _ in range(generator.randrange(1, 8)))
            cluster = "/projects" + below
            with self.subTest(cluster=cluster):
                self.assertEqual(collection_path(cluster, ROOT), below)
                # Under the root "/" nothing changes.
                self.assertEqual(collection_path(cluster, "/"), cluster)


class CheckClusterFolder(ArcCase):
    def assertValid(self, path, root=ROOT):
        self.assertEqual(check_cluster_folder(path, root), [], path)

    def assertRefused(self, path, *words, root=ROOT):
        problems = check_cluster_folder(path, root)
        self.assertTrue(problems, "%r was accepted" % (path,))
        for problem in problems:
            self.assertIsInstance(problem, str)
            # A sentence that names the setting, never an example person.
            self.assertIn("ARC folder", problem)
        text = " ".join(problems)
        for word in words:
            self.assertIn(word, text)
        return problems

    def test_the_default_and_other_team_folders_are_valid(self):
        for path in (DEFAULT_FOLDER, "/projects/pcl_photonics/raman", "/projects/a/b", "/projects/A-1/b.c_d",
                     "/projects/x/y/z/deeper", "/projects/" + "a" * 64 + "/" + "b" * 64):
            with self.subTest(path=path):
                self.assertValid(path)

    def test_the_root_and_a_project_directory_alone_are_refused(self):
        self.assertRefused("/projects", "inside a project directory", "/projects alone is not allowed")
        self.assertRefused("/projects/pcl_photonics", "inside a project directory")

    def test_outside_the_root(self):
        self.assertRefused("/home/someone/uploads", "not inside /projects")
        self.assertRefused("/projectsx/a/b", "not inside /projects")
        self.assertRefused("/scratch/a/b", "not inside /projects")

    def test_relative_empty_and_not_text(self):
        self.assertRefused("projects/a/b", "must start with /")
        self.assertRefused("", "is empty")
        self.assertRefused(None, "is empty")
        self.assertRefused(42, "is empty")
        self.assertEqual(len(check_cluster_folder("", ROOT)), 1)

    def test_trailing_slashes_dots_and_empty_parts(self):
        for path in ("/projects/a/b/", "/projects/a//b", "//projects/a/b", "/projects/../x/y", "/projects/x/../y",
                     "/projects/x/./y", "/projects/x/.y", "/projects/x/..", "/projects/.hidden/y"):
            with self.subTest(path=path):
                self.assertRefused(path, "not allowed")

    def test_refused_characters(self):
        for part in ("-y", "_y", "a b", "a;b", "a$b", "a'b", 'a"b', "a\\b", "a*", "a?b", "~a", "a:b", "été",
                     "a\tb", "a\x00b", "a\x7fb", "y\n", "y\r", "\ny"):
            path = "/projects/x/" + part
            with self.subTest(path=path):
                self.assertRefused(path, "not allowed")

    def test_a_newline_at_the_end_is_refused(self):
        # In Python '$' matches before a newline at the end: FOLDER_PART.match() alone would take it.
        self.assertTrue(FOLDER_PART.match("raman_globus_upload\n"))
        self.assertFalse(FOLDER_PART.fullmatch("raman_globus_upload\n"))
        self.assertRefused(DEFAULT_FOLDER + "\n", "not allowed")

    def test_long_names(self):
        self.assertValid("/projects/x/" + "a" * 64)
        self.assertRefused("/projects/x/" + "a" * 65, "at most 64")
        long_path = "/projects" + "/abcdefghi" * 19
        self.assertEqual(len(long_path), 199)
        self.assertValid(long_path)
        self.assertValid(long_path + "j")
        self.assertRefused(long_path + "jk", "longer than 200")
        self.assertRefused(long_path + "/j", "longer than 200")

    def test_another_root(self):
        self.assertValid("/projects/a", root="/")
        self.assertRefused("/projects", "inside a project directory", root="/")
        self.assertValid("/work/team/folder", root="/work")
        self.assertRefused(DEFAULT_FOLDER, "not inside /work", root="/work")

    def test_problems_name_no_person(self):
        problems = []
        for path in ("", "x", "/projects", "/home/a/b", "/projects/a/-b", "/projects/x/" + "a" * 300):
            problems.extend(check_cluster_folder(path, ROOT))
        text = " ".join(problems)
        # Only placeholders appear in the examples, no account, user or host name.
        self.assertIn("/projects/<project-directory>/<folder>", text)
        self.assertNotIn("@", text)


class RelativeBelow(ArcCase):
    def setUp(self):
        super().setUp()
        self.tmp = support.make_home(self)
        self.real = self.tmp / "real"
        (self.real / "pcl_photonics" / "raman" / "work").mkdir(parents=True)
        # ARC's /projects/<dir> may be a link; arc_job resolves the work folder (review F-04).
        (self.tmp / "projects").symlink_to(self.real, target_is_directory=True)

    def test_a_folder_below_the_root(self):
        work = self.real / "pcl_photonics" / "raman" / "work"
        self.assertEqual(relative_below(work, self.real), "pcl_photonics/raman/work")
        self.assertEqual(relative_below(str(work), str(self.real)), "pcl_photonics/raman/work")
        self.assertEqual(relative_below(self.real, self.real), "")
        # A folder that does not exist yet (the job makes its download folders later).
        self.assertEqual(relative_below(work / "download-x", self.real), "pcl_photonics/raman/work/download-x")

    def test_a_linked_root_gives_the_same_answer_from_either_side(self):
        linked = self.tmp / "projects"
        work = "pcl_photonics/raman/work"
        self.assertEqual(relative_below(linked / work, linked), work)
        self.assertEqual(relative_below(self.real / work, linked), work)
        self.assertEqual(relative_below(linked / work, self.real), work)
        self.assertEqual(relative_below(linked, self.real), "")

    def test_a_link_that_leads_out_of_the_root_is_outside(self):
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        (self.real / "pcl_photonics" / "out").symlink_to(elsewhere, target_is_directory=True)
        with self.assertRaises(ValueError):
            relative_below(self.real / "pcl_photonics" / "out", self.real)

    def test_outside_the_root(self):
        for path in (self.tmp, self.tmp / "realx", Path("/"), self.real / ".." / "elsewhere"):
            with self.subTest(path=path):
                with self.assertRaises(ValueError) as raised:
                    relative_below(path, self.real)
                self.assertIn("is not inside", str(raised.exception))


class DemoLocalPath(ArcCase):
    def test_the_demo_folder_mirrors_the_cluster_path(self):
        demo = Path("/tmp/home/data_catalog/arc_demo")
        self.assertEqual(demo_local_path(demo, DEFAULT_FOLDER),
                         demo / "projects" / "pcl_photonics" / "raman_globus_upload")
        self.assertEqual(demo_local_path(str(demo), DEFAULT_FOLDER + "/simulated/s1"),
                         demo / "projects/pcl_photonics/raman_globus_upload/simulated/s1")
        self.assertEqual(demo_local_path(demo, "/"), demo)
        self.assertIsInstance(demo_local_path(str(demo), "/a"), Path)

    def test_the_demo_globus_collection_maps_its_paths_through_the_same_rule(self):
        # The demo collection shows demo_local_path(root, collection_root + "/X") as "/X" (section 5).
        demo = Path("/tmp/home/data_catalog/arc_demo")
        globus = collection_path(DEFAULT_FOLDER, ROOT)
        self.assertEqual(demo_local_path(demo, ROOT + globus), demo_local_path(demo, DEFAULT_FOLDER))

    def test_a_dot_dot_part_never_leaves_the_demo_folder(self):
        for path in ("/projects/../../etc", "/..", "../x", "/projects/x/.."):
            with self.subTest(path=path):
                with self.assertRaises(ValueError):
                    demo_local_path("/tmp/demo", path)


class Module(ArcCase):
    def test_standard_library_only(self):
        # The ARC job imports this module on the cluster, and the arc-* commands at start-up.
        source = Path(arc_paths.__file__).read_text(encoding="utf-8")
        imports = sorted({line.split()[1].split(".")[0] for line in source.splitlines()
                          if line.startswith(("import ", "from ")) and not line.startswith("from __future__")})
        self.assertEqual(imports, ["os", "pathlib", "re"])


if __name__ == "__main__":
    unittest.main()
