"""Case templates: what a new case's ``main.typ`` (the case map) says before anyone writes in it.

A template is only text for the map's sections: the question, the line to try first, the
sources to collect and what is still open. ``blank`` is the generic map. The others state a
plain question from Danish law, named by act and without section numbers, so the map is
something to correct rather than something to fill in. Each is Typst markup.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Template:
    key: str
    name: str  # the case name when none is given
    question: str
    main: str
    defeat: str
    left_alone: str
    collect: tuple[str, ...]  # the sources to collect; none for blank
    open: tuple[str, ...]


BLANK = Template(
    key="blank",
    name="",
    question="_Replace this paragraph with the question the case has to answer._\n\nWhat has to be decided, and for whom.",
    main="The claim the first memo will test.",
    defeat="The fact or the rule that ends the line.",
    left_alone="What this case is not arguing.",
    collect=(),
    open=("The question above is still a placeholder.", "No labels yet, so no quotes."),
)

TEMPLATES: dict[str, Template] = {
    t.key: t
    for t in (
        BLANK,
        Template(
            key="rent",
            name="Rent increase",
            question="The landlord has written that the monthly rent goes up. Was the increase validly notified under the Danish Rent Act (lejeloven), so that the tenant has to pay it?",
            main="The notice does not meet the act's requirements of form and content, so the increase has not taken effect.",
            defeat="The notice is in writing, states the amount and the reason, and arrived in time.",
            left_alone="Whether the new rent is reasonable in amount.",
            collect=(
                "The lease.",
                "The landlord's letter that announces the increase.",
                "The Rent Act: the rules on rent increases and on notice.",
            ),
            open=("The date the tenant received the letter.", "Whether the tenant has objected, and when."),
        ),
        Template(
            key="family",
            name="Contact arrangement",
            question="The parents disagree on how often the child is with the parent it does not live with. What contact arrangement should that parent ask for under the Parental Responsibility Act (forældreansvarsloven)?",
            main="Regular contact is in the child's best interest, and the request builds on the contact the child already has.",
            defeat="A concrete reason that more contact would harm the child.",
            left_alone="Who has custody, and where the child lives.",
            collect=(
                "The current agreement or decision on contact.",
                "The parents' correspondence about contact.",
                "The Parental Responsibility Act: the rules on contact and on the child's best interest.",
            ),
            open=("The child's age, and what the child has said it wants.", "Whether the family law agency (Familieretshuset) is already involved."),
        ),
        Template(
            key="tax",
            name="Tax reassessment",
            question="The tax agency has changed the assessment of an earlier year. Was the change made within the deadline in the Tax Administration Act (skatteforvaltningsloven)?",
            main="The change came after the ordinary deadline, and no exception applies.",
            defeat="The taxpayer gave wrong or incomplete information, which allows a later change.",
            left_alone="Whether the new amount is right.",
            collect=(
                "The decision that changes the assessment.",
                "The original assessment for that year.",
                "The advance notice of the change (agterskrivelse), if there was one.",
                "The Tax Administration Act: the rules on deadlines for reassessment.",
            ),
            open=("The date of the advance notice.", "Whether the agency relies on an exception to the deadline."),
        ),
    )
}


def get(key: str) -> Template:
    try:
        return TEMPLATES[key or "blank"]
    except KeyError:
        raise ValueError(f"unknown template {key!r}: one of {', '.join(TEMPLATES)}") from None


def label(t: Template) -> str:
    return t.name or "Blank"
