"""Query de-duplication and follow-up routing."""

from app.graph.build import similar, unique_queries


def test_near_duplicate_queries_are_dropped():
    qs = ["Duc-Anh Nguyen AWS challenge generative AI race strategy solutions",
          "AWS challenge generative AI race strategy solutions Duc-Anh Nguyen",
          "AWS challenge fan engagement solutions"]
    assert unique_queries(qs) == [qs[0], qs[2]]


def test_second_round_does_not_repeat_the_first():
    first = ["AWS challenge generative AI race strategy solutions"]
    assert unique_queries(["generative AI race strategy AWS challenge solutions"], first) == []
    assert unique_queries(["AWS challenge global broadcasting solutions"], first) != []


def test_different_questions_are_kept():
    assert not similar("What did he do at ZEISS?", "Which hackathons did he join?")
