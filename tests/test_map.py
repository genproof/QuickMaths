"""Plan-vs-Lean map: workspace preparation, the Lean template and the per-lane summary (offline)."""
import json
import tempfile
import unittest
from pathlib import Path

from harness import cli
from harness.map import build, summary

MAP = "\n".join([
    # name, module, own sorry, sorry-dependent, reached by the final theorem
    "Submission.hole\tSubmission\t0\t1\t1",
    "Submission.Lanes.Tool.main\tSubmission.Lanes.Tool.Main\t0\t0\t1",
    "Submission.Lanes.Tool.contDiff_pd\tSubmission.Lanes.Tool.Calc\t0\t0\t1",
    "Submission.Lanes.Case.main\tSubmission.Lanes.Case.Main\t0\t1\t1",
    "Submission.Lanes.Case.step\tSubmission.Lanes.Case.Step\t1\t1\t1",
    "Submission.Lanes.Case.contDiff_pd\tSubmission.Lanes.Case.Calc\t0\t0\t0",
    "Submission.Lanes.Case.unused\tSubmission.Lanes.Case.Old\t1\t1\t0",
    "Submission.Lanes.Case.main._proof_1\tSubmission.Lanes.Case.Main\t0\t0\t1",
    "Submission.Lanes.Vac.main\tSubmission.Lanes.Vac.Main\t0\t0\t1",
    "Submission.Lanes.Vac.not_toolStrong\tSubmission.Lanes.Vac.Refute\t0\t0\t1\tSubmission.Lanes.ToolStatement",
    "Submission.Lanes.Case.not_isBounded_ray\tSubmission.Lanes.Case.Calc\t0\t0\t1\t-",
]) + "\n"


class MapTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_summary_finds_what_is_left_dead_code_duplicates_and_refutations(self):
        path = self.base / "map.tsv"
        path.write_text(MAP)
        result = summary.summarize(summary.load(path), "Submission.hole", {"Tool": {"done": True}})
        self.assertTrue(result["final_found"] and result["final_depends_on_sorry"])
        self.assertEqual([r["name"] for r in result["whats_left"]], ["Submission.Lanes.Case.step"])
        self.assertEqual(result["refutations_in_final_proof"], ["Submission.Lanes.Vac.not_toolStrong"])
        self.assertEqual(result["refuted_statements"], {"Submission.Lanes.ToolStatement": ["Submission.Lanes.Vac.not_toolStrong"]})
        case = result["parts"]["Case"]
        self.assertEqual((case["decls"], case["reached"], case["dead"], case["own_sorry"]), (5, 3, 2, 2))
        self.assertEqual(result["parts"]["Tool"]["status"], "done")
        self.assertIn(["contDiff_pd", ["Case", "Tool"]], result["duplicates"])
        self.assertIn("what's left: 1 declarations", summary.text(result))

    def test_template_names_the_final_theorem(self):
        text = build.map_lean("Submission.cerf_gamma_four")
        self.assertIn("let final := `Submission.cerf_gamma_four", text)
        self.assertIn("import Submission\n", text)
        self.assertNotIn("{{", text)
        self.assertIn("let refuted := refutes.getD n", text)
        self.assertNotIn("\t", text)

    def fake_workbench(self) -> Path:
        wb = self.base / "wb"
        template = wb / "vendor/lean-eval/generated/demo"
        template.mkdir(parents=True)
        for name in build.TEMPLATE_FILES:
            (template / name).write_text(f"-- {name}\n")
        (template / "holes.json").write_text(json.dumps({"holes": ["demo_hole"]}))
        (wb / "vendor/lean-eval/lake-manifest.json").write_text('{"packages": []}\n')
        (wb / "vendor/lean-eval/.lake/packages/mathlib").mkdir(parents=True)
        return wb

    def test_prepare_links_the_packages_and_keeps_existing_files(self):
        wb = self.fake_workbench()
        tree = self.base / "tree"
        (tree / "Submission").mkdir(parents=True)
        (tree / "Submission.lean").write_text("theorem demo_hole : True := trivial\n")
        (tree / "lakefile.toml").write_text("-- mine\n")
        template = build.prepare(tree, "demo", wb)
        self.assertEqual((tree / "lakefile.toml").read_text(), "-- mine\n")
        self.assertEqual((tree / "lean-toolchain").read_text(), "-- lean-toolchain\n")
        self.assertTrue((tree / ".lake/packages").is_symlink())
        self.assertEqual((tree / ".lake/packages").resolve(), (wb / "vendor/lean-eval/.lake/packages").resolve())
        self.assertEqual(build.default_final(template, "demo"), "Submission.demo_hole")
        build.prepare(tree, "demo", wb)  # idempotent: the link is kept, not re-created inside the packages
        self.assertFalse((wb / "vendor/lean-eval/.lake/packages/packages").exists())

    def test_prepare_refuses_a_tree_without_submission(self):
        with self.assertRaises(SystemExit):
            build.prepare(self.base, "demo", self.fake_workbench())

    def test_cli_summarize(self):
        path = self.base / "map.tsv"
        path.write_text(MAP)
        self.assertEqual(cli.main(["map", "summarize", str(path), "--final", "Submission.hole"]), 0)
        self.assertEqual(cli.main(["map"]), 2)


if __name__ == "__main__":
    unittest.main()
