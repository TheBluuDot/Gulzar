"""NastaliqKerning plugin

This plugin provides two FEZ verbs - `NastaliqKerning` and
`AtHeight`. These are related in that they compute the "height"
of a glyph sequence and chain to a different routine based on
that height. In the case of `NastaliqKerning`, we evaluate all
sequences beginning with an initial glyph, compute the height
of that sequence, and also create a kerning table for that height.
In `AtHeight`, we evaluate all sequences, and if they fall within
a specified height range, dispatch to an arbitrary routine. (This
is used for the dot avoidance code.)

The syntax of each verb is:

    NastaliqKerning <units> <percentage>%

Creates kern tables which bring together the initial and final
glyphs to within `units` units of one another or within the specified
percentage of the width of the final glyph.

You will want to read https://simoncozens.github.io/nastaliq-autokerning/
before trying to understand this code.

    AtHeight <units1>-<units2> <routine>

Chains to the given routine for all sequences of glyphs between
`units1` and `units2` high. The context of the chained routine will
be the final/isolate glyph of the previous sequence, a space (if there
is one), and the glyph sequence. For example, in the case of the
sequence `لو پجل`, the height will be computed as 400 units in the
case of Gulzar; if 400 is between `units1` and `units2`, then the
routine will be called with `VAOf1` as the start of the chained glyph
sequence.
"""
import fontFeatures
import sys
from glyphtools import bin_glyphs_by_metric, get_glyph_metrics
from itertools import product, chain
import warnings
import math
import bidict
import tqdm
from functools import lru_cache
from fez import FEZVerb
from beziers.path import BezierPath
from beziers.point import Point
import shelve
import logging
import re

from qalamTools.determinekern import determine_kern


PARSEOPTS = dict(use_helpers=True)

GRAMMAR = ""

NastaliqKerning_GRAMMAR="""
?start: action
action: integer_container integer_container "%"
"""

AtHeight_GRAMMAR="""
?start: action
action: integer_container "-" integer_container BARENAME
"""

VERBS = ["NastaliqKerning", "AtHeight"]

logging.basicConfig(format='%(message)s')
logger = logging.getLogger("NastaliqKerning")
logger.setLevel(logging.WARN)

# Number of different "rise" groups, used in clustering the glyphs
# to determine the sequence height. This is O(n^2) in number of
# generated layout rules, so increasing this to 4 will overflow
# the font builder! 3 seems accurate enough for practical purposes.
accuracy1 = 3

# Controls the number of kern tables to be generated by rounding
# the computed height to this number of units. i.e. at baseline,
# at height of 100 units, at height of 200 units, etc. - up to...
rise_quantization = 100
# the maximum height we care about. All higher sequences will have
# the same kerning applied to them.
maximum_rise = 600

# Rounding the kern values allows them to be stored more efficiency
# in the OpenType binary.
kern_quantization = 10

# Only consider sequences of this length - for longer sequences,
# we start counting height from the *medial* instead of the final.
maximum_word_length = 5

class Hashabledict(dict):
    def __hash__(self):
        return hash(frozenset(self))


def quantize(number, degree):
    return degree * round(number / degree)


class NastaliqKerning(FEZVerb):
    def action(self, args):
        # Read the parameters
        self.distance_at_closest = args[0].resolve_as_integer()
        self.maxtuck = args[1].resolve_as_integer() / 100.0
        self.ink_to_ink_routines = {}

        # Computing kern values takes a long time, so we cache
        # the computed values in this file.
        self.shelve = shelve.open("kerncache.db")

        # Read a few useful classes into Python variables.
        self.inits = self.parser.fontfeatures.namedClasses["inits"]
        medis = self.parser.fontfeatures.namedClasses["medis"]
        bariye = self.parser.fontfeatures.namedClasses["bariye"]
        self.isols = [x for x in self.parser.fontfeatures.namedClasses["isols"] if x not in bariye]
        finas = [x for x in self.parser.fontfeatures.namedClasses["finas"] if x not in bariye]
        self.isols_finas = list(set(self.isols + finas) | set(bariye))

        # These glyphs are special cased. We should probably read
        # `blockers` from a glyph class, really, instead of hard
        # coding it.
        blockers = ["AINf1", "JIMf1"]

        # Now we cluster the medials and finals based on their
        # rise.
        binned_medis = bin_glyphs_by_metric(
            self.parser.font, medis, "rise", bincount=accuracy1
        )
        binned_finas = bin_glyphs_by_metric(
            self.parser.font, finas, "rise", bincount=accuracy1
        )

        # This will hold kern tables for each rise value.
        self.kern_at_rise = {}
        routines = []

        # The main entry to our kerning routine. We ignore marks
        # and ligatures (spaces)
        routine = fontFeatures.Routine(name="NastaliqKerning")
        routine.flags = 0x04 | 0x08

        routines.append(routine)

        # We will build our word sequences, from longest to shortest.
        # `i` will count medial and final glyphs, not including the
        # initial glyph, which is why we go down to zero.
        for i in range(maximum_word_length, -1, -1):
            postcontext_options = [binned_finas] + [binned_medis] * i
            warnings.warn("Length "+str(i))

            # This iterator returns all sequences of glyph groups.
            # For example, when `i` is 2 it will return
            #    binned_finas[0] binned_medis[0] binned_medis[0]
            #    binned_finas[0] binned_medis[0] binned_medis[1]
            #    binned_finas[0] binned_medis[0] binned_medis[2]
            #    binned_finas[0] binned_medis[1] binned_medis[0]
            #    binned_finas[0] binned_medis[1] binned_medis[1]
            #    ...
            #    binned_finas[2] binned_medis[2] binned_medis[2]
            all_options = product(*postcontext_options)

            for postcontext_plus_rise in all_options:
                # Each group is a two-element tuple: the glyphs in
                # the group and the median rise for each group. By
                # summing the second element of each group, we get
                # the height of this sequence.

                # This is a bit of a hack :-(
                # if len(postcontext_plus_rise) > 1:
                word_tail_rise = quantize(
                    sum([x[1] for x in postcontext_plus_rise]), rise_quantization
                )
                # else:
                    # word_tail_rise = 0
                if word_tail_rise < 0:
                    continue

                # And by reading the first element, we get the glyphs
                # involved.
                postcontext = list(reversed([x[0] for x in postcontext_plus_rise]))
                # warnings.warn("%s - %i" % (postcontext, word_tail_rise))
                if word_tail_rise >= maximum_rise:
                    word_tail_rise = maximum_rise
                    if i == maximum_word_length:
                        # Drop the fina, so that we match all sequence
                        # starting with these glyphs.
                        postcontext.pop()

                # The right hand side of our glyph pair
                target = [self.isols_finas]
                lookups = [[self.generate_kern_table_for_rise(word_tail_rise)]]

                # Are there any blocking final glyphs in this sequence?
                do_blockers = False
                if any(blocker in postcontext[-1] for blocker in blockers):
                    # If so, remove them from the group and handle them later;
                    # by unconditionally separating them from the group, we
                    # keep the groups constant across the whole lookup, which
                    # allows them to be represented as a format 2 lookup
                    # which is very efficient.
                    postcontext[-1] = list(set(postcontext[-1]) - set(blockers))
                    do_blockers = True

                # Call the appropriate kern table for this sequence
                routine.rules.append(
                    fontFeatures.Chaining(
                        target,
                        postcontext=[self.inits] + postcontext,
                        lookups=lookups,
                    )
                )

                # We now deal with blocking glyphs. If the sequence length
                # is 1 (init + final), skip it; otherwise, add it.
                if len(postcontext) > 1 and do_blockers:
                    postcontext[-1] = blockers
                    routine.rules.append(
                        fontFeatures.Chaining(
                            target,
                            postcontext=[self.inits] + postcontext,
                            lookups=lookups,
                        )
                    )

                if word_tail_rise >= 400 and i > 4:
                    # HACK
                    # This has to be done separately to make the classes work
                    postcontext[-1] = ["BARI_YEf1"]
                    routine.rules.append(
                        fontFeatures.Chaining(
                            target,
                            postcontext=[self.inits] + postcontext,
                            lookups=lookups,
                        )
                    )


        # Finally, kern isolates against each other.
        target = [self.isols_finas]
        lookups = [[self.generate_kern_table_for_rise(0)]]
        routine.rules.append(
            fontFeatures.Chaining(
                target,
                lookups=lookups,
                postcontext = [self.isols]
            )
        )

        # And we're done.
        self.shelve.close()
        return routines

    def determine_kern_cached(self, font, glyph1, glyph2, targetdistance, height, maxtuck=0.4):
        # Determines the kern; the heavy lifting is done in
        # qalamTools.determinekern, we just cache the result.
        key = "/".join(str(x) for x in [glyph1, glyph2, targetdistance, height, maxtuck])
        if key in self.shelve:
            return self.shelve[key]
        result = determine_kern(font, glyph1, glyph2, targetdistance, height, maxtuck=maxtuck)
        self.shelve[key] = result
        return result


    def ink_to_ink_at(self, r):
        if r in self.ink_to_ink_routines:
            return self.ink_to_ink_routines[r]

        # Taper distance based on height to make it visually equal!
        r_distance = self.distance_at_closest
        if r == 100:
            r_distance *= 0.5
        if r == 200:
            r_distance *= 0.2
        if r == 300:
            r_distance *= 0.2

        ink_to_ink = fontFeatures.Routine("ink_to_ink_%i" % r, flags=0x8|0x4)
        font = self.parser.font
        for right in self.isols_finas:
            for left in self.inits + self.isols:
                right_of_left = max(font.glyphs[left].layers[0].rsb, 0)
                left_of_right = max(font.glyphs[right].layers[0].lsb, 0)
                dist = int(r_distance - (right_of_left + left_of_right))
                if dist == 0:
                    continue
                ink_to_ink.rules.append(
                    fontFeatures.Positioning(
                        [ [right], [left] ],
                        [
                            fontFeatures.ValueRecord(),
                            fontFeatures.ValueRecord(xAdvance=dist),
                        ],
                    )
                )
        self.ink_to_ink_routines[r] = self.parser.fontfeatures.referenceRoutine(ink_to_ink)
        return self.ink_to_ink_routines[r]


    def generate_kern_table_for_rise(self, r):
        if r in self.kern_at_rise:
            return self.kern_at_rise[r]
        r = quantize(r, rise_quantization)
        kerntable = {}

        print("Generating table for rise %s" %r, file=sys.stderr)
        # At the baseline, the left glyph of the sequence is all the
        # isolates and initials; but if there is a rise, we must
        # have seen a medial/final before it so we ignore the isolates.
        if r > 0:
            ends = self.inits
        else:
            ends = self.inits + self.isols

        maxtuck = self.maxtuck

        # So this is easy; we just go through every combination and
        # determine the kern.
        with tqdm.tqdm(total=len(ends) * len(self.isols_finas), miniters=30) as pbar:
            for end_of_previous_word in self.isols_finas:
                kerntable[end_of_previous_word] = {}
                for initial in sorted(ends): # initial of "long" sequence, i.e. left glyph
                    logger.info("Left glyph: %s" % initial)
                    logger.info("Right glyph: %s" % end_of_previous_word)
                    kern = self.determine_kern_cached(
                        self.parser.font,
                        initial,
                        end_of_previous_word,
                        self.distance_at_closest,
                        height=r,
                        maxtuck=maxtuck,
                    )# - max(get_glyph_metrics(self.parser.font, initial)["rsb"],0)
                    logger.info("%s - %s @ %i : %i" % (initial, end_of_previous_word, r, kern))
                    # Only record a kern if we are actually bringing two glyphs closer.
                    if kern < -10:
                        kerntable[end_of_previous_word][initial] = quantize(kern, kern_quantization)
                    pbar.update(1)

        # Once we've done so, we stick it in a pair positioning routine.
        kernroutine = fontFeatures.Routine(
            rules=[],
            name="kern_at_%i" % r,
        )
        kernroutine.flags=0x08 | 0x04
        abovemarks = self.parser.fontfeatures.namedClasses["all_above_marks"]
        kernroutine.markFilteringSet=abovemarks

        for left, kerns in kerntable.items():
            for right, value in kerns.items():
                kernroutine.rules.append(
                    fontFeatures.Positioning(
                        [ [left], [right] ],
                        [
                            fontFeatures.ValueRecord(),
                            fontFeatures.ValueRecord(xAdvance=value),
                        ],
                    )
                )
        kernroutine = self.parser.fontfeatures.referenceRoutine(kernroutine)
        kernroutine._table = kerntable

        # This kern routine is going to dispatch differently depending on
        # a) height and b) whether or not there is a space.
        # if the height is >= 400, then everyone gets this kind of kerning,
        # space or not
        # But if the height is less than 400, we branch into two separate
        # kern tables: the "ink-to-ink" table if there is a space, or the
        # table we just made otherwise.
        if r >= 400:
            self.kern_at_rise[r] = kernroutine
            return kernroutine
        dispatch = fontFeatures.Routine(name="dispatch_%i" % r, flags=0x8)
        dispatch.rules.append(
            fontFeatures.Chaining(
                [self.isols_finas, ["space.urdu"], ends],
                lookups=[[self.ink_to_ink_at(r)],[],[]]
            )
        )
        dispatch.rules.append(
            fontFeatures.Chaining(
                [self.isols_finas, ends],
                lookups=[[kernroutine],[],[]]
            )
        )
        self.kern_at_rise[r] = dispatch
        return dispatch

# This is just a generic version of the above.
class AtHeight(FEZVerb):
    def action(self, args):
        (height_lower, height_upper, target_routine) = args
        height_lower = height_lower.resolve_as_integer();
        height_upper = height_upper.resolve_as_integer();
        target_routine = self.parser.fontfeatures.routineNamed(target_routine)
        self.inits = self.parser.fontfeatures.namedClasses["inits"]
        medis = self.parser.fontfeatures.namedClasses["medis"]
        isols = self.parser.fontfeatures.namedClasses["isols"]
        finas = self.parser.fontfeatures.namedClasses["finas"]

        self.isols_finas = isols + finas

        binned_medis = bin_glyphs_by_metric(
            self.parser.font, medis, "rise", bincount=accuracy1
        )
        binned_finas = bin_glyphs_by_metric(
            self.parser.font, finas, "rise", bincount=accuracy1
        )

        routine = fontFeatures.Routine(name="At_%s_%s_%s" % (height_lower, height_upper, target_routine.name))
        routine.flags = 0x04 | 0x08

        for i in range(maximum_word_length, -1, -1):
            postcontext_options = [binned_finas] + [binned_medis] * i
            all_options = product(*postcontext_options)
            for postcontext_plus_rise in all_options:
                word_tail_rise = quantize(
                    sum([x[1] for x in postcontext_plus_rise]), rise_quantization
                )
                postcontext = list(reversed([x[0] for x in postcontext_plus_rise]))
                if not (word_tail_rise >= height_lower and word_tail_rise <= height_upper):
                    continue

                target = [self.isols_finas, self.inits]
                lookups = [[target_routine]] + [None] * (len(target)-1)
                routine.rules.append(
                    fontFeatures.Chaining(
                        target,
                        postcontext=postcontext,
                        lookups=lookups,
                    )
                )
        return [routine]




