"""Phase 7 — measure the extractor against the gold set.

Runs the extractor over the text held in `tests/fixtures/gold_set.yaml` and
compares what it produces with what the fixture says the text supports. No
network, no database, no API key: the fixture carries the text, so the number
is reproducible by anyone with the repo.

## What precision and recall mean here

The fixture is sampled by **item**, not by extractor output. Forty of its
records are gate-passed items drawn uniformly at random, including the ones
the extractor found nothing in and the ones that report no move at all. That
is what makes recall measurable: a fixture assembled from records the
extractor produced can only ever confirm what it already got right, and would
score near 100% by construction while saying nothing about what was missed.

    precision = matched / predicted     did we invent moves?
    recall    = matched / expected      did we find the ones that are there?

A predicted move matches an expected one when they name the same person, by
the same normalisation dedupe blocks on. Field accuracy is then measured only
over matched pairs, because a field on an unmatched move is not a wrong field,
it is a wrong move, and counting it twice flatters nothing.

Firms are compared through the gazetteer. The fixture records the surface the
article used, since `expect` has to be readable out of the text; the extractor
emits the canonical name. "Allen & Overy" and "Allen & Overy Shearman" are the
same destination and are scored as agreement.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from tracker import names
from tracker.extract.extractor import RawItem
from tracker.extract.rules import RuleExtractor
from tracker.firms import FirmGazetteer

GOLD_PATH = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "gold_set.yaml"

# The brief's bar. `tracker evaluate` fails below it.
PRECISION_BAR = 0.98

# Fields compared over matched pairs. `person_name` is the matching key, so it
# is not scored here — a matched pair agrees on it by construction.
SCORED_FIELDS = ("to_firm", "from_firm", "title_to", "office_jurisdiction")
FIRM_FIELDS = frozenset({"to_firm", "from_firm"})


def _person_key(name: str | None) -> str:
    """Match key: surname plus given name, ordering-normalised."""
    if not name:
        return ""
    return f"{names.surname_key(name)}|{names.given_key(name) or ''}"


@dataclass
class FieldScore:
    agreed: int = 0
    compared: int = 0
    # Values the gold set states and the extractor left null. Not a
    # disagreement about the world -- a field it declined to read.
    missed: int = 0

    @property
    def accuracy(self) -> float:
        return self.agreed / self.compared if self.compared else 0.0


@dataclass
class Report:
    records: int = 0
    expected: int = 0
    predicted: int = 0
    matched: int = 0
    # Records the fixture says report no move, where the extractor made one.
    false_movements: int = 0
    negatives: int = 0
    fields: dict = field(default_factory=dict)
    misses: list = field(default_factory=list)
    spurious: list = field(default_factory=list)

    @property
    def precision(self) -> float:
        return self.matched / self.predicted if self.predicted else 0.0

    @property
    def recall(self) -> float:
        return self.matched / self.expected if self.expected else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0


def _same_firm(gazetteer: FirmGazetteer, one: str | None, other: str | None) -> bool:
    if one is None or other is None:
        return one is other
    if one.strip().lower() == other.strip().lower():
        return True
    a, b = gazetteer.resolve(one), gazetteer.resolve(other)
    return a is not None and a == b


def _same_text(one: str | None, other: str | None) -> bool:
    if one is None or other is None:
        return one is other
    return one.strip().lower() in other.strip().lower() or (
        other.strip().lower() in one.strip().lower()
    )


def run(gold_path: Path = GOLD_PATH) -> Report:
    gold = yaml.safe_load(gold_path.read_text(encoding="utf-8"))
    gazetteer = FirmGazetteer.load()
    extractor = RuleExtractor(gazetteer=gazetteer)

    report = Report()
    report.fields = {name: FieldScore() for name in SCORED_FIELDS}

    for record in gold["records"]:
        report.records += 1
        text = record["headline"]
        if record.get("text"):
            text += "\n\n" + record["text"]

        item = RawItem(
            source_slug=record["source_slug"],
            url=record.get("url") or "",
            headline=record["headline"],
            published_at=record.get("published_at"),
            access_level=record.get("access_level") or "summary",
            body_text=record.get("text"),
        )
        result = extractor.extract(item, reliability_tier=record["reliability_tier"])

        expected = record["expect"]["moves"]
        predicted = list(result.moves)
        report.expected += len(expected)
        report.predicted += len(predicted)

        if not expected:
            report.negatives += 1
            if predicted:
                report.false_movements += 1
                report.spurious.append(
                    (record["id"], [m.value("person_name") for m in predicted])
                )
            continue

        by_key = {_person_key(m.value("person_name")): m for m in predicted}
        for want in expected:
            got = by_key.pop(_person_key(want["person_name"]), None)
            if got is None:
                report.misses.append((record["id"], want["person_name"]))
                continue
            report.matched += 1
            for name in SCORED_FIELDS:
                score = report.fields[name]
                gold_value, got_value = want.get(name), got.value(name)
                if gold_value is None and got_value is None:
                    continue
                if gold_value is not None and got_value is None:
                    score.missed += 1
                    score.compared += 1
                    continue
                score.compared += 1
                same = (
                    _same_firm(gazetteer, gold_value, got_value)
                    if name in FIRM_FIELDS
                    else _same_text(gold_value, got_value)
                )
                if same:
                    score.agreed += 1
        # Anything left in by_key was predicted for this record and expected by
        # nobody: a person the text does not put in a move.
        for person in by_key:
            report.spurious.append((record["id"], [person]))

    return report


def render(report: Report) -> str:
    lines = [
        "",
        f"  gold records          {report.records}"
        f"  ({report.negatives} reporting no move)",
        f"  moves expected        {report.expected}",
        f"  moves predicted       {report.predicted}",
        f"  matched               {report.matched}",
        "",
        f"  precision             {report.precision:.3f}",
        f"  recall                {report.recall:.3f}",
        f"  f1                    {report.f1:.3f}",
        "",
        "  field agreement over matched pairs",
    ]
    for name, score in report.fields.items():
        lines.append(
            f"    {name:20} {score.accuracy:.3f}"
            f"   ({score.agreed}/{score.compared} agreed,"
            f" {score.missed} left null)"
        )
    lines.append("")
    lines.append(
        f"  records the fixture says report no move, "
        f"where a move was made: {report.false_movements}"
    )
    if report.spurious:
        lines.append("  moves predicted that the text does not support:")
        for record_id, people in report.spurious[:8]:
            lines.append(f"    {record_id}: {people}")
    if report.misses:
        lines.append(f"  moves in the text that were not found: {len(report.misses)}")
        for record_id, person in report.misses[:10]:
            lines.append(f"    {record_id}: {person}")
    lines.append("")
    return "\n".join(lines)
