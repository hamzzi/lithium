# coding=utf-8
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Lithium reduction strategy implementations"""

import abc
import functools
import hashlib
import logging
import re
import time

from .util import (
    divide_rounding_up,
    is_power_of_two,
    largest_power_of_two_smaller_than,
    quantity,
    summary_header,
)

DEFAULT = "minimize"
LOG = logging.getLogger(__name__)


class ReductionIterator(abc.ABC):
    """Iterator over a reduction strategy.

    Each iteration should be attempted against the target, and `feedback` should be
    called to update the testcase accordingly. The end result can be obtained using
    `best`.
    """

    def __init__(self, testcase):
        self._best_testcase = testcase
        self._testcase_attempt = None
        self._any_success = False
        self._last_success = None
        self._description = "Reduction"
        self._tried = set()

    @property
    def last_feedback(self):
        """Get the feedback value from the latest attempt.

        Returns:
            bool: The value last passed to `self.feedback()`
        """
        assert self._last_success is not None, "No feedback received yet"
        return self._last_success

    def update_tried(self, tried):
        """Update the list of tried hashes. Testcases are hashed with SHA-512
        and digested to bytes (`hashlib.sha512(testcase).digest()`)

        Args:
            tried (iterable(str)): Set of already tried testcase hashes.

        Returns:
            None
        """
        self._tried.update(frozenset(tried))

    def get_tried(self):
        """Return the set of tried testcase hashes. Testcases are hashed with SHA-512
        and digested to bytes (`hashlib.sha512(testcase).digest()`)

        Returns:
            frozenset(str): Testcase hashes.
        """
        return frozenset(self._tried)

    def feedback(self, success):
        """Provide feedback on the current reduction attempt.

        Args:
            success (bool): Whether or not the current reduction was "successful".
        """
        assert self._testcase_attempt is not None, "No testcase being attempted"
        assert self._last_success is None, "Already got feedback"
        self._last_success = success
        if success:
            self._best_testcase = self._testcase_attempt
            self._any_success = True
        self._testcase_attempt = None

    def try_testcase(self, testcase, description="Reduction"):
        """Update the currently attempted testcase.

        Args:
            testcase (Testcase): The testcase to try.

        Yields:
            Testcase: same as argument
        """
        assert self._testcase_attempt is None, "Already attempting a testcase"
        # de-dupe the testcase
        # include before/after since different testcase types
        #   may split them inconsistently.
        tc_hash = hashlib.sha512()
        tc_hash.update(testcase.before)
        for part in testcase.parts:
            tc_hash.update(part)
        tc_hash.update(testcase.after)
        tc_hash = tc_hash.hexdigest()
        if tc_hash not in self._tried:
            self._tried.add(tc_hash)
            self._last_success = None
            self._testcase_attempt = testcase
            self._description = description
            yield self._testcase_attempt

    @property
    def testcase(self):
        """Get the best successful testcase in this reduction.

        Returns:
            Testcase: The current best testcase (or end result, if finished).
        """
        return self._best_testcase

    @property
    def reduced(self):
        """Check whether any reduction has been successful.

        Returns:
            bool: True if any iteration got successful feedback.
        """
        return self._any_success

    @property
    def description(self):
        """Describe the reduction attempt.

        Returns:
            str: Description of the current reduction.
        """
        return self._description

    @abc.abstractmethod
    def __iter__(self):
        """Attempt to reduce this testcase.

        Yields:
            Testcase: Reduction attempts. The caller must call `feedback()` following
                      each result yielded.
        """

    @classmethod
    def wrap(cls, method):
        """This can be used as a decorator to define the `Strategy.reduce` method
        with a simpler signature:

            def reduce(self, iterator):
                yield testcase

        Args:
            method: The reduce method to wrap

        Returns:
            callable: The method wrapped with the signature for `Strategy.reduce`
        """

        @functools.wraps(method)
        def wrapped(inst, testcase):
            class _iter(cls):
                def __iter__(self):
                    yield from method(inst, self)

            return _iter(testcase)

        return wrapped


class Strategy(abc.ABC):
    """Abstract minimization strategy class

    Implementers should define a main() method which takes a testcase and calls the
    interesting callback repeatedly to minimize the testcase.
    """

    def add_args(self, parser):
        """Add extra strategy-specific arguments to an ArgumentParser.

        Args:
            parser (ArgumentParser): argparse instance to add arguments to.
        """

    def process_args(self, parser, args):
        """Handle any args added by this strategy in `add_args()`

        Args:
            parser (argparse.ArgumentParser): argparse instance, if errors need to be
                                              raised.
            args (argparse.Namespace): parsed args to process.
        """

    @abc.abstractmethod
    def reduce(self, testcase):
        """

        Args:
            testcase (Testcase): testcase to reduce

        Returns:
            Iterable: An iterable to reduce the testcase (see ReductionIterator).
        """

    def main(self, testcase, interesting, temp_filename):
        """

        Args:
            testcase (Testcase): Testcase to reduce.
            interesting (callback): Callback to test a potential reduction. The callback
                should return True if the reduction is good, and False otherwise. This
                usually involves launching an external target to evaluate the testcase.
                The callback has the following signature:

                def interesting(testcase, write_it=True):

                    Args:
                        testcase (Testcase): reduction to test
                        write_it (bool): whether the interestingness test should write
                                         the testcase to disk.

                    Returns:
                        bool: Whether the condition was observed when evaluating the
                              given testcase.
            temp_filename (callback): Create a temporary filename for the next testcase.
                The callback has the following signature:

                def temp_filename(filename_stem, use_number=True):

                    Args:
                        filename_stem (str): Basename for the testcase on disk.
                        use_number (bool): Prefix filename with the next number in
                                           sequence.

                    Returns:
                        Path: Filename to use for the next testcase.

        Returns:
            int: 0 on success
        """
        testcase.dump(temp_filename("original", False))

        if not testcase:
            LOG.info(
                "The file has %s so there's nothing for Lithium to try to remove!",
                quantity(0, testcase.atom),
            )
            return 0

        orig_len = quantity(len(testcase), testcase.atom)
        LOG.info("The original testcase has %s.", orig_len)

        LOG.info("Checking that the original testcase is 'interesting'...")
        if not interesting(testcase, write_it=False):
            LOG.info("Lithium result: the original testcase is not 'interesting'!")
            return 1

        reduction = self.reduce(testcase)
        for attempt in reduction:
            success = interesting(attempt)
            if success:
                LOG.info("%s was successful", reduction.description)
            else:
                LOG.info("%s made the file uninteresting", reduction.description)
            reduction.feedback(success)
        # write the final best testcase to disk
        testcase = reduction.testcase
        testcase.dump()

        summary_header()

        LOG.info("  Initial size: %s", orig_len)
        LOG.info("  Final size: %s", quantity(len(testcase), testcase.atom))

        return int(not reduction.reduced)


class CheckOnly(Strategy):
    """Only check whether the testcase reproduces."""

    name = "check-only"

    @ReductionIterator.wrap
    def reduce(self, iterator):  # pylint: disable=arguments-differ
        # check doesn't reduce, only checks
        yield from iterator.try_testcase(iterator.testcase, "Check")

    def main(self, testcase, interesting, temp_filename):
        result = interesting(testcase, write_it=False)
        LOG.info("Lithium result: %sinteresting.", ("" if result else "not "))
        return int(not result)


class Minimize(Strategy):
    """Main reduction algorithm

    This strategy attempts to remove chunks which might not be interesting
    code, but which can be removed independently of any other.  This happens
    frequently with values which are computed, but either after the execution,
    or never used to influenced the interesting part.

      a = compute();
      b = compute();   <-- !!!
      interesting(a);
      c = compute();   <-- !!!"""

    name = "minimize"

    def __init__(self):
        super().__init__()
        self.minimize_repeat = "last"
        self.minimize_min = 1
        self.minimize_max = pow(2, 30)
        self.minimize_chunk_size = None
        self.minimize_repeat_first_round = False
        self.stop_after_time = None

    def add_args(self, parser):
        super().add_args(parser)
        grp_add = parser.add_argument_group(
            description="Additional options for the %s strategy" % (self.name,)
        )
        grp_add.add_argument(
            "--min", type=int, default=1, help="must be a power of two. default: 1"
        )
        grp_add.add_argument(
            "--max",
            type=int,
            default=pow(2, 30),
            help="must be a power of two. default: about half of the file",
        )
        grp_add.add_argument(
            "--repeat",
            default="last",
            choices=["always", "last", "never"],
            help="Whether to repeat a chunk size if chunks are removed. default: last",
        )
        grp_add.add_argument(
            "--chunk-size",
            type=int,
            default=None,
            help="Shortcut for repeat=never, min=n, max=n. chunk size must be a power "
            "of two.",
        )
        grp_add.add_argument(
            "--repeat-first-round",
            action="store_true",
            help="Treat the first round as if it removed chunks; possibly repeat it. "
            "[Mostly intended for internal use]",
        )
        grp_add.add_argument(
            "--max-run-time",
            type=int,
            default=None,
            help="If reduction takes more than n seconds, stop (and print instructions "
            "for continuing).",
        )

    def process_args(self, parser, args):
        super().process_args(parser, args)
        if args.chunk_size:
            self.minimize_min = args.chunk_size
            self.minimize_max = args.chunk_size
            self.minimize_repeat = "never"
        else:
            self.minimize_min = args.min
            self.minimize_max = args.max
            self.minimize_repeat = args.repeat
        self.minimize_repeat_first_round = args.repeat_first_round
        if args.max_run_time:
            self.stop_after_time = args.max_run_time
        if not is_power_of_two(self.minimize_min):
            parser.error("Min must be a power of two.")
        if not is_power_of_two(self.minimize_max):
            parser.error("Max must be a power of two.")

    @staticmethod
    def _post_round_cb(iterator):
        return []

    @ReductionIterator.wrap
    def reduce(self, iterator):  # pylint: disable=arguments-differ
        chunk_size = min(
            self.minimize_max, largest_power_of_two_smaller_than(len(iterator.testcase))
        )
        min_chunk_size = min(chunk_size, max(self.minimize_min, 1))
        chunk_end = len(iterator.testcase)
        removed_chunks = self.minimize_repeat_first_round
        stop_after_time = None
        if self.stop_after_time is not None:
            stop_after_time = time.time() + self.stop_after_time

        while True:
            if stop_after_time is not None and time.time() > stop_after_time:
                LOG.warning(
                    "Lithium result: run time elapsed, please perform another pass "
                    "using the same arguments"
                )
                return

            if chunk_end - chunk_size < 0:
                # If the testcase is empty, end minimization
                if not iterator.testcase:
                    LOG.info(
                        "Lithium result: succeeded, reduced to: %s",
                        quantity(len(iterator.testcase), iterator.testcase.atom),
                    )
                    break

                yield from self._post_round_cb(iterator)

                # If the chunk_size is less than or equal to the min_chunk_size and...
                if chunk_size <= min_chunk_size:
                    # Repeat mode is last or always and at least one chunk was removed
                    # during the last round, repeat
                    if removed_chunks and (
                        self.minimize_repeat == "always"
                        or self.minimize_repeat == "last"
                    ):
                        LOG.info("Starting another round of chunk size %d", chunk_size)
                        chunk_end = len(iterator.testcase)
                    # Otherwise, end minimization
                    else:
                        LOG.info(
                            "Lithium result: succeeded, reduced to: %s",
                            quantity(len(iterator.testcase), iterator.testcase.atom),
                        )
                        break
                # If none of the conditions apply, reduce the chunk_size and continue
                else:
                    chunk_end = len(iterator.testcase)
                    while chunk_size > 1:  # smallest valid chunk size is 1
                        chunk_size >>= 1
                        # To avoid testing with an empty testcase (wasting cycles) only
                        # break when chunk_size is less than the number of testcase
                        # parts available.
                        if chunk_size < len(iterator.testcase):
                            break

                    LOG.info("")
                    LOG.info("Reducing chunk size to %d", chunk_size)
                removed_chunks = False

            chunk_start = max(0, chunk_end - chunk_size)
            status = "Removing chunk from %s to %s of %d" % (
                chunk_start,
                chunk_end,
                len(iterator.testcase),
            )
            test_to_try = iterator.testcase.copy()
            test_to_try.parts = (
                test_to_try.parts[:chunk_start] + test_to_try.parts[chunk_end:]
            )
            for test in iterator.try_testcase(test_to_try, status):
                yield test
                if iterator.last_feedback:
                    removed_chunks = True
                    chunk_end = chunk_start
                    break
            else:
                # Decrement chunk_end
                # To ensure the file is fully reduced, decrement chunk_end by 1 when
                # chunk_size <= 2
                if chunk_size <= 2:
                    chunk_end -= 1
                else:
                    chunk_end -= chunk_size

        if chunk_size == 1 and not removed_chunks and self.minimize_repeat != "never":
            LOG.info(
                "  Removing any single %s from the final file makes it uninteresting!",
                iterator.testcase.atom,
            )


class MinimizeSurroundingPairs(Minimize):
    """This strategy attempts to remove pairs of chunks which might be surrounding
    interesting code, but which cannot be removed independently of the other.
    This happens frequently with patterns such as:

      a = 42;
      while (true) {
         b = foo(a);      <-- !!!
         interesting();
         a = bar(b);      <-- !!!
      }"""

    name = "minimize-around"

    @ReductionIterator.wrap
    def reduce(self, iterator):
        chunk_size = min(
            self.minimize_max, largest_power_of_two_smaller_than(len(iterator.testcase))
        )
        final_chunk_size = max(self.minimize_min, 1)
        stop_after_time = None
        if self.stop_after_time is not None:
            stop_after_time = time.time() + self.stop_after_time

        while True:
            any_chunks_removed = False
            for testcase in self.try_removing_chunks(
                chunk_size, stop_after_time, iterator
            ):
                yield testcase
                any_chunks_removed = any_chunks_removed or iterator.last_feedback

            if stop_after_time is not None and time.time() > stop_after_time:
                # Not all switches will be copied!
                # Be sure to add --tempdir, --maxruntime if desired.
                LOG.warning(
                    "Lithium result: run time elapsed, please perform another pass "
                    "using the same arguments"
                )
                return

            last = chunk_size <= final_chunk_size

            if any_chunks_removed and (
                self.minimize_repeat == "always"
                or (self.minimize_repeat == "last" and last)
            ):
                # Repeat with the same chunk size
                continue

            if last:
                # Done
                break

            # Continue with the next smaller chunk size
            chunk_size >>= 1

        if final_chunk_size == 1 and self.minimize_repeat != "never":
            LOG.info(
                "  Removing any single %s from the final file makes it uninteresting!",
                iterator.testcase.atom,
            )

    @staticmethod
    def try_removing_chunks(chunk_size, stop_after_time, iterator):
        """Make a single run through the testcase, trying to remove chunks of size
        chunk_size.

        Yields:
            Testcase: attempts to remove chunks
        """

        chunks_removed = 0
        atoms_removed = 0

        atoms_initial = len(iterator.testcase)
        num_chunks = divide_rounding_up(len(iterator.testcase), chunk_size)

        # Not enough chunks to remove surrounding blocks.
        if num_chunks < 3:
            return

        LOG.info(
            "Starting a round with chunks of %s.",
            quantity(chunk_size, iterator.testcase.atom),
        )

        summary = "S" * num_chunks
        chunk_start = chunk_size
        before_chunk_idx = 0
        keep_chunk_idx = 1
        after_chunk_idx = 2

        try:
            while chunk_start + chunk_size < len(iterator.testcase):
                if stop_after_time is not None and time.time() > stop_after_time:
                    return

                chunk_bef_start = max(0, chunk_start - chunk_size)
                chunk_bef_end = chunk_start
                chunk_aft_start = min(len(iterator.testcase), chunk_start + chunk_size)
                chunk_aft_end = min(
                    len(iterator.testcase), chunk_aft_start + chunk_size
                )
                description = "Removing chunk #%d & #%d of %d chunks of size %d" % (
                    before_chunk_idx,
                    after_chunk_idx,
                    num_chunks,
                    chunk_size,
                )

                testcase_suggestion = iterator.testcase.copy()
                testcase_suggestion.parts = (
                    testcase_suggestion.parts[:chunk_bef_start]
                    + testcase_suggestion.parts[chunk_bef_end:chunk_aft_start]
                    + testcase_suggestion.parts[chunk_aft_end:]
                )
                for test in iterator.try_testcase(testcase_suggestion, description):
                    yield test
                    if iterator.last_feedback:
                        chunks_removed += 2
                        atoms_removed += chunk_bef_end - chunk_bef_start
                        atoms_removed += chunk_aft_end - chunk_aft_start
                        summary = (
                            summary[:before_chunk_idx]
                            + "-"
                            + summary[before_chunk_idx + 1 :]
                        )
                        summary = (
                            summary[:after_chunk_idx]
                            + "-"
                            + summary[after_chunk_idx + 1 :]
                        )
                        # The start is now sooner since we remove the chunk which was
                        # before this one.
                        chunk_start -= chunk_size
                        try:
                            # Try to keep removing surrounding chunks of the same part.
                            before_chunk_idx = summary.rindex("S", 0, keep_chunk_idx)
                        except ValueError:
                            # There is no more survinving block on the left-hand-side of
                            # the current chunk, shift everything by one surviving
                            # block. Any ValueError from here means that there is no
                            # longer enough chunk.
                            before_chunk_idx = keep_chunk_idx
                            keep_chunk_idx = summary.index("S", keep_chunk_idx + 1)
                            chunk_start += chunk_size
                        break
                else:
                    # Shift chunk indexes, and seek the next surviving chunk. ValueError
                    # from here means that there is no longer enough chunks.
                    before_chunk_idx = keep_chunk_idx
                    keep_chunk_idx = after_chunk_idx
                    chunk_start += chunk_size

                after_chunk_idx = summary.index("S", keep_chunk_idx + 1)

        except ValueError:
            # This is a valid loop exit point.
            pass

        atoms_surviving = atoms_initial - atoms_removed
        printable_summary = " ".join(
            summary[(2 * i) : min(2 * (i + 1), num_chunks + 1)]
            for i in range(num_chunks // 2 + num_chunks % 2)
        )
        LOG.info("")
        LOG.info("Done with a round of chunk size %d!", chunk_size)
        LOG.info(
            "%s survived; %s removed.",
            quantity(summary.count("S"), "chunk"),
            quantity(summary.count("-"), "chunk"),
        )
        LOG.info(
            "%s survived; %s removed.",
            quantity(atoms_surviving, iterator.testcase.atom),
            quantity(atoms_removed, iterator.testcase.atom),
        )
        LOG.info("Which chunks survived: %s", printable_summary)
        LOG.info("")


class MinimizeBalancedPairs(MinimizeSurroundingPairs):
    """This strategy attempts to remove balanced chunks which might be surrounding
    interesting code, but which cannot be removed independently of the other.
    This happens frequently with patterns such as:

      ...;
      if (cond) {        <-- !!!
         ...;
         interesting();
         ...;
      }                  <-- !!!
      ...;

    The value of the condition might not be interesting, but in order to reach the
    interesting code we still have to compute it, and keep extra code alive."""

    name = "minimize-balanced"

    def __init__(self):
        super().__init__()
        self.use_experimental_move = False

    def add_args(self, parser):
        super().add_args(parser)
        grp_add = parser.add_argument_group(
            description="Additional options for the %s strategy" % (self.name,)
        )
        grp_add.add_argument(
            "--with-experimental-move",
            action="store_true",
            help="Moving chunks is still a bit experimental, and it can introduce "
            "reducing loops. Use at own risk!",
        )

    def process_args(self, parser, args):
        super().process_args(parser, args)
        self.use_experimental_move = args.with_experimental_move

    def try_removing_chunks(self, chunk_size, stop_after_time, iterator):
        """Make a single run through the testcase, trying to remove chunks of size
        chunk_size.

        Yields:
            Testcase: attempts to remove chunks
        """

        chunks_removed = 0
        atoms_removed = 0

        atoms_initial = len(iterator.testcase)
        num_chunks = divide_rounding_up(len(iterator.testcase), chunk_size)

        # Not enough chunks to remove surrounding blocks.
        if num_chunks < 2:
            return

        LOG.info(
            "Starting a round with chunks of %s.",
            quantity(chunk_size, iterator.testcase.atom),
        )

        def _count_diff(chunk, ops):
            assert len(ops) == 2
            return iterator.testcase.parts[chunk].count(
                ops[0]
            ) - iterator.testcase.parts[chunk].count(ops[1])

        summary = "S" * num_chunks
        curly = [_count_diff(i, b"{}") for i in range(num_chunks)]
        square = [_count_diff(i, b"[]") for i in range(num_chunks)]
        normal = [_count_diff(i, b"()") for i in range(num_chunks)]
        chunk_start = 0
        lhs_chunk_idx = 0

        try:
            while chunk_start < len(iterator.testcase):
                if stop_after_time is not None and time.time() > stop_after_time:
                    return

                description = "chunk #%d of %d chunks of size %d" % (
                    lhs_chunk_idx,
                    num_chunks,
                    chunk_size,
                )

                assert (
                    summary.count("S", 0, lhs_chunk_idx) * chunk_size == chunk_start
                ), (
                    "the chunk_start should correspond to the lhs_chunk_idx modulo the "
                    "removed chunks."
                )

                chunk_lhs_start = chunk_start
                chunk_lhs_end = min(
                    len(iterator.testcase), chunk_lhs_start + chunk_size
                )

                n_curly = curly[lhs_chunk_idx]
                n_square = square[lhs_chunk_idx]
                n_normal = normal[lhs_chunk_idx]

                # If the chunk is already balanced, try to remove it.
                if not (n_curly or n_square or n_normal):
                    testcase_suggestion = iterator.testcase.copy()
                    testcase_suggestion.parts = (
                        testcase_suggestion.parts[:chunk_lhs_start]
                        + testcase_suggestion.parts[chunk_lhs_end:]
                    )
                    for test in iterator.try_testcase(
                        testcase_suggestion, "Removing " + description
                    ):
                        yield test
                        if iterator.last_feedback:
                            chunks_removed += 1
                            atoms_removed += chunk_lhs_end - chunk_lhs_start
                            summary = (
                                summary[:lhs_chunk_idx]
                                + "-"
                                + summary[lhs_chunk_idx + 1 :]
                            )
                            break
                    else:
                        chunk_start += chunk_size
                    lhs_chunk_idx = summary.index("S", lhs_chunk_idx + 1)
                    continue

                # Otherwise look for the corresponding chunk.
                rhs_chunk_idx = lhs_chunk_idx
                for item in summary[lhs_chunk_idx + 1 :]:
                    rhs_chunk_idx += 1
                    if item != "S":
                        continue
                    n_curly += curly[rhs_chunk_idx]
                    n_square += square[rhs_chunk_idx]
                    n_normal += normal[rhs_chunk_idx]
                    if n_curly < 0 or n_square < 0 or n_normal < 0:
                        break
                    if not (n_curly or n_square or n_normal):
                        break

                # If we have no match, then just skip this pair of chunks.
                if n_curly or n_square or n_normal:
                    LOG.info("Skipping %s because it is 'uninteresting'.", description)
                    chunk_start += chunk_size
                    lhs_chunk_idx = summary.index("S", lhs_chunk_idx + 1)
                    continue

                # Otherwise we do have a match and we check if this is interesting to
                # remove both.
                chunk_rhs_start = chunk_lhs_start + chunk_size * summary.count(
                    "S", lhs_chunk_idx, rhs_chunk_idx
                )
                chunk_rhs_start = min(len(iterator.testcase), chunk_rhs_start)
                chunk_rhs_end = min(
                    len(iterator.testcase), chunk_rhs_start + chunk_size
                )

                description = "chunk #%d & #%d of %d chunks of size %d" % (
                    lhs_chunk_idx,
                    rhs_chunk_idx,
                    num_chunks,
                    chunk_size,
                )

                testcase_suggestion = iterator.testcase.copy()
                testcase_suggestion.parts = (
                    testcase_suggestion.parts[:chunk_lhs_start]
                    + testcase_suggestion.parts[chunk_lhs_end:chunk_rhs_start]
                    + testcase_suggestion.parts[chunk_rhs_end:]
                )
                worked = False
                for test in iterator.try_testcase(
                    testcase_suggestion, "Removing " + description
                ):
                    yield test
                    if iterator.last_feedback:
                        chunks_removed += 2
                        atoms_removed += chunk_lhs_end - chunk_lhs_start
                        atoms_removed += chunk_rhs_end - chunk_rhs_start
                        summary = (
                            summary[:lhs_chunk_idx] + "-" + summary[lhs_chunk_idx + 1 :]
                        )
                        summary = (
                            summary[:rhs_chunk_idx] + "-" + summary[rhs_chunk_idx + 1 :]
                        )
                        lhs_chunk_idx = summary.index("S", lhs_chunk_idx + 1)
                        worked = True
                if worked:
                    continue

                # Removing the braces make the failure disappear.  As we are looking
                # for removing chunk (braces), we need to make the content within
                # the braces as minimal as possible, so let us try to see if we can
                # move the chunks outside the braces.

                if not self.use_experimental_move:
                    chunk_start += chunk_size
                    lhs_chunk_idx = summary.index("S", lhs_chunk_idx + 1)
                    continue

                # Moving chunks is still a bit experimental, and it can introduce
                # reducing loops.

                def _split_parts(lst, step, ignore_before, start, stop):
                    return (
                        lst[:ignore_before],
                        lst[ignore_before:start],
                        lst[start : (start + step)],
                        lst[(start + step) : (stop + step)],
                        lst[(stop + step) :],
                    )

                def _parts_after(*args):
                    assert len(args) == 5
                    return args[0] + args[1] + args[3] + args[2] + args[4]

                def _parts_before(*args):
                    assert len(args) == 5
                    return args[0] + args[2] + args[1] + args[3] + args[4]

                def _move_after(*args):
                    return _parts_after(*_split_parts(*args))

                def _move_before(*args):
                    return _parts_after(*_split_parts(*args))

                orig_chunk_idx = lhs_chunk_idx
                stay_on_same_chunk = False
                chunk_mid_start = chunk_lhs_end
                mid_chunk_idx = summary.index("S", lhs_chunk_idx + 1)
                while chunk_mid_start < chunk_rhs_start:
                    assert (
                        summary.count("S", 0, mid_chunk_idx) * chunk_size
                        == chunk_mid_start
                    ), (
                        "the chunk_mid_start should correspond to the mid_chunk_idx "
                        "modulo the removed chunks."
                    )
                    description = "chunk #%d of %d chunks of size %d" % (
                        mid_chunk_idx,
                        num_chunks,
                        chunk_size,
                    )

                    parts = _split_parts(
                        iterator.testcase.parts,
                        chunk_size,
                        chunk_lhs_start,
                        chunk_mid_start,
                        chunk_rhs_start,
                    )

                    n_curly = curly[mid_chunk_idx]
                    n_square = square[mid_chunk_idx]
                    n_normal = normal[mid_chunk_idx]
                    if n_curly or n_square or n_normal:
                        LOG.info(
                            "Keeping %s because it is 'uninteresting'.", description
                        )
                        chunk_mid_start += chunk_size
                        mid_chunk_idx = summary.index("S", mid_chunk_idx + 1)
                        continue

                    # Try moving the chunk after.
                    testcase_suggestion = iterator.testcase.copy()
                    testcase_suggestion.parts = _parts_after(parts)
                    worked = False
                    for test in iterator.try_testcase(
                        testcase_suggestion, "->Moving " + description
                    ):
                        yield test
                        if iterator.last_feedback:
                            chunk_rhs_start -= chunk_size
                            chunk_rhs_end -= chunk_size
                            summary = _move_after(
                                summary, 1, lhs_chunk_idx, mid_chunk_idx, rhs_chunk_idx
                            )
                            curly = _move_after(
                                curly, 1, lhs_chunk_idx, mid_chunk_idx, rhs_chunk_idx
                            )
                            square = _move_after(
                                square, 1, lhs_chunk_idx, mid_chunk_idx, rhs_chunk_idx
                            )
                            normal = _move_after(
                                normal, 1, lhs_chunk_idx, mid_chunk_idx, rhs_chunk_idx
                            )
                            rhs_chunk_idx -= 1
                            mid_chunk_idx = summary.index("S", mid_chunk_idx + 1)
                            worked = True
                    if worked:
                        continue

                    # Try moving the chunk before.
                    testcase_suggestion.parts = _parts_before(parts)
                    worked = False
                    for test in iterator.try_testcase(
                        testcase_suggestion, "<-Moving " + description
                    ):
                        yield test
                        if iterator.last_feedback:
                            chunk_lhs_start += chunk_size
                            chunk_lhs_end += chunk_size
                            chunk_mid_start += chunk_size
                            summary = _move_before(
                                summary, 1, lhs_chunk_idx, mid_chunk_idx, rhs_chunk_idx
                            )
                            curly = _move_before(
                                curly, 1, lhs_chunk_idx, mid_chunk_idx, rhs_chunk_idx
                            )
                            square = _move_before(
                                square, 1, lhs_chunk_idx, mid_chunk_idx, rhs_chunk_idx
                            )
                            normal = _move_before(
                                normal, 1, lhs_chunk_idx, mid_chunk_idx, rhs_chunk_idx
                            )
                            lhs_chunk_idx += 1
                            mid_chunk_idx = summary.index("S", mid_chunk_idx + 1)
                            stay_on_same_chunk = True
                            worked = True
                    if worked:
                        continue

                    chunk_mid_start += chunk_size
                    mid_chunk_idx = summary.index("S", mid_chunk_idx + 1)

                lhs_chunk_idx = orig_chunk_idx
                if not stay_on_same_chunk:
                    chunk_start += chunk_size
                    lhs_chunk_idx = summary.index("S", lhs_chunk_idx + 1)

        except ValueError:
            # This is a valid loop exit point.
            pass

        atoms_surviving = atoms_initial - atoms_removed
        printable_summary = " ".join(
            summary[(2 * i) : min(2 * (i + 1), num_chunks + 1)]
            for i in range(num_chunks // 2 + num_chunks % 2)
        )
        LOG.info("")
        LOG.info("Done with a round of chunk size %d!", chunk_size)
        LOG.info(
            "%s survived; %s removed.",
            quantity(summary.count("S"), "chunk"),
            quantity(summary.count("-"), "chunk"),
        )
        LOG.info(
            "%s survived; %s removed.",
            quantity(atoms_surviving, iterator.testcase.atom),
            quantity(atoms_removed, iterator.testcase.atom),
        )
        LOG.info("Which chunks survived: %s", printable_summary)
        LOG.info("")


class ReplacePropertiesByGlobals(Minimize):
    """This strategy attempts to remove members, such that other strategies can
    then move the lines outside the functions.  The goal is to rename
    variables at the same time, such that the program remains valid, while
    removing the dependency on the object on which the member is part of.

      function Foo() {
        this.list = [];
      }
      Foo.prototype.push = function(a) {
        this.list.push(a);
      }
      Foo.prototype.last = function() {
        return this.list.pop();
      }

    Which might transform the previous example to something like:

      function Foo() {
        list = [];
      }
      push = function(a) {
        list.push(a);
      }
      last = function() {
        return list.pop();
      }"""

    name = "replace-properties-by-globals"

    @ReductionIterator.wrap
    def reduce(self, iterator):
        chunk_size = min(
            self.minimize_max,
            2 * largest_power_of_two_smaller_than(len(iterator.testcase)),
        )
        final_chunk_size = max(self.minimize_min, 1)

        orig_num_chars = 0
        for line in iterator.testcase.parts:
            orig_num_chars += len(line)

        num_chars = orig_num_chars
        while True:
            num_removed_chars = 0
            for maybe_removed, testcase in self.try_making_globals(
                chunk_size, num_chars, iterator
            ):
                yield testcase
                if iterator.last_feedback:
                    num_removed_chars += maybe_removed

            num_chars -= num_removed_chars

            last = chunk_size <= final_chunk_size

            if num_removed_chars and (
                self.minimize_repeat == "always"
                or (self.minimize_repeat == "last" and last)
            ):
                # Repeat with the same chunk size
                pass
            elif last:
                # Done
                break
            else:
                # Continue with the next smaller chunk size
                chunk_size >>= 1

        LOG.info("  Initial size: %s", quantity(orig_num_chars, "character"))
        LOG.info("  Final size: %s", quantity(num_chars, "character"))

        if final_chunk_size == 1 and self.minimize_repeat != "never":
            LOG.info(
                "  Removing any single %s from the final file makes it uninteresting!",
                iterator.testcase.atom,
            )

    def try_making_globals(self, chunk_size, num_chars, iterator):
        """Make a single run through the testcase, trying to remove chunks of size
        chunk_size.

        Returns True iff any chunks were removed."""

        num_removed_chars = 0
        num_chunks = divide_rounding_up(len(iterator.testcase), chunk_size)
        final_chunk_size = max(self.minimize_min, 1)

        # Map words to the chunk indexes in which they are present.
        words = {}
        for chunk, line in enumerate(iterator.testcase.parts):
            for match in re.finditer(br"(?<=[\w\d_])\.(\w+)", line):
                word = match.group(1)
                if word not in words:
                    words[word] = [chunk]
                else:
                    words[word] += [chunk]

        # All patterns have been removed successfully.
        if not words:
            return

        LOG.info(
            "Starting a round with chunks of %s.",
            quantity(chunk_size, iterator.testcase.atom),
        )
        summary = "S" * num_chunks

        for word, chunks in list(words.items()):
            chunk_indexes = {}
            for chunk_start in chunks:
                chunk_idx = chunk_start // chunk_size
                if chunk_idx not in chunk_indexes:
                    chunk_indexes[chunk_idx] = [chunk_start]
                else:
                    chunk_indexes[chunk_idx] += [chunk_start]

            for chunk_idx, chunk_starts in chunk_indexes.items():
                # Unless this is the final size, let's try to remove couple of
                # prefixes, otherwise wait for the final size to remove each of them
                # individually.
                if len(chunk_starts) == 1 and final_chunk_size != chunk_size:
                    continue

                description = "'%s' in chunk #%d of %d chunks of size %d" % (
                    word.decode("utf-8", "replace"),
                    chunk_idx,
                    num_chunks,
                    chunk_size,
                )

                maybe_removed = 0
                new_tc = iterator.testcase.copy()
                for chunk_start in chunk_starts:
                    subst = re.sub(
                        br"[\w_.]+\." + word, word, new_tc.parts[chunk_start]
                    )
                    maybe_removed += len(new_tc.parts[chunk_start]) - len(subst)
                    new_tc.parts = (
                        new_tc.parts[:chunk_start]
                        + [subst]
                        + new_tc.parts[(chunk_start + 1) :]
                    )

                for test in iterator.try_testcase(
                    new_tc, "Removing prefixes of " + description
                ):
                    yield maybe_removed, test
                    if iterator.last_feedback:
                        num_removed_chars += maybe_removed
                        summary = summary[:chunk_idx] + "s" + summary[chunk_idx + 1 :]
                        words[word] = [c for c in chunks if c not in chunk_indexes]
                        if not words[word]:
                            del words[word]

        num_surviving_chars = num_chars - num_removed_chars
        printable_summary = " ".join(
            summary[(2 * i) : min(2 * (i + 1), num_chunks + 1)]
            for i in range(num_chunks // 2 + num_chunks % 2)
        )
        LOG.info("")
        LOG.info("Done with a round of chunk size %d!", chunk_size)
        LOG.info(
            "%s survived; %s shortened.",
            quantity(summary.count("S"), "chunk"),
            quantity(summary.count("s"), "chunk"),
        )
        LOG.info(
            "%s survived; %s removed.",
            quantity(num_surviving_chars, "character"),
            quantity(num_removed_chars, "character"),
        )
        LOG.info("Which chunks survived: %s", printable_summary)
        LOG.info("")


class ReplaceArgumentsByGlobals(Minimize):
    """This strategy attempts to replace arguments by globals, for each named
    argument of a function we add a setter of the global of the same name before
    the function call.  The goal is to remove functions by making empty arguments
    lists instead.

      function foo(a,b) {
        list = a + b;
      }
      foo(2, 3)

    becomes:

      function foo() {
        list = a + b;
      }
      a = 2;
      b = 3;
      foo()

    The next logical step is inlining the body of the function at the call site."""

    name = "replace-arguments-by-globals"

    @ReductionIterator.wrap
    def reduce(self, iterator):
        while True:
            num_removed_arguments = 0
            for maybe_removed, testcase in self.try_arguments_as_globals(iterator):
                yield testcase
                if iterator.last_feedback:
                    num_removed_arguments += maybe_removed

            if num_removed_arguments and (
                self.minimize_repeat == "always" or self.minimize_repeat == "last"
            ):
                # Repeat with the same chunk size
                pass
            else:
                # Done
                break

    @staticmethod
    def try_arguments_as_globals(iterator):
        """Make a single run through the testcase, trying to remove chunks of size
        chunk_size.

        Returns True iff any chunks were removed."""

        num_moved_arguments = 0
        num_survived_arguments = 0

        # Map words to the chunk indexes in which they are present.
        functions = {}
        anonymous_queue = []
        anonymous_stack = []
        for chunk, line in enumerate(iterator.testcase.parts):
            # Match function definition with at least one argument.
            for match in re.finditer(
                br"(?:function\s+(\w+)|(\w+)\s*=\s*function)\s*"
                br"\((\s*\w+\s*(?:,\s*\w+\s*)*)\)",
                line,
            ):
                fun = match.group(1)
                if fun is None:
                    fun = match.group(2)

                if match.group(3) == b"":
                    args = []
                else:
                    args = match.group(3).split(b",")

                if fun not in functions:
                    functions[fun] = {
                        "defs": args,
                        "args_pattern": match.group(3),
                        "chunk": chunk,
                        "uses": [],
                    }
                else:
                    functions[fun]["defs"] = args
                    functions[fun]["args_pattern"] = match.group(3)
                    functions[fun]["chunk"] = chunk

            # Match anonymous function definition, which are surrounded by parentheses.
            for match in re.finditer(
                br"\(function\s*\w*\s*\(((?:\s*\w+\s*(?:,\s*\w+\s*)*)?)\)\s*{", line
            ):
                if match.group(1) == b"":
                    args = []
                else:
                    args = match.group(1).split(",")
                anonymous_stack += [
                    {"defs": args, "chunk": chunk, "use": None, "use_chunk": 0}
                ]

            # Match calls of anonymous function.
            for match in re.finditer(br"}\s*\)\s*\(((?:[^()]|\([^,()]*\))*)\)", line):
                if not anonymous_stack:
                    continue
                anon = anonymous_stack[-1]
                anonymous_stack = anonymous_stack[:-1]
                if match.group(1) == b"" and not anon["defs"]:
                    continue
                if match.group(1) == b"":
                    args = []
                else:
                    args = match.group(1).split(b",")
                anon["use"] = args
                anon["use_chunk"] = chunk
                anonymous_queue += [anon]

            # match function calls. (and some definitions)
            for match in re.finditer(br"((\w+)\s*\(((?:[^()]|\([^,()]*\))*)\))", line):
                pattern = match.group(1)
                fun = match.group(2)
                if match.group(3) == b"":
                    args = []
                else:
                    args = match.group(3).split(b",")
                if fun not in functions:
                    functions[fun] = {"uses": []}
                functions[fun]["uses"] += [
                    {"values": args, "chunk": chunk, "pattern": pattern}
                ]

        # All patterns have been removed successfully.
        if not functions and not anonymous_queue:
            return

        LOG.info("Starting removing function arguments.")

        for fun, args_map in functions.items():
            description = "arguments of '" + fun.decode("utf-8", "replace") + "'"
            if "defs" not in args_map or not args_map["uses"]:
                LOG.info("Ignoring %s because it is 'uninteresting'.", description)
                continue

            maybe_moved_arguments = 0
            new_tc = iterator.testcase.copy()

            # Remove the function definition arguments
            arg_defs = args_map["defs"]
            def_chunk = args_map["chunk"]
            subst = new_tc.parts[def_chunk].replace(args_map["args_pattern"], b"", 1)
            new_tc.parts = (
                new_tc.parts[:def_chunk] + [subst] + new_tc.parts[(def_chunk + 1) :]
            )

            # Copy callers arguments to globals.
            for arg_use in args_map["uses"]:
                values = arg_use["values"]
                chunk = arg_use["chunk"]
                if chunk == def_chunk and values == arg_defs:
                    continue
                while len(values) < len(arg_defs):
                    values = values + [b"undefined"]
                setters = b"".join(
                    (a + b" = " + v + b";\n") for (a, v) in zip(arg_defs, values)
                )
                subst = setters + new_tc.parts[chunk]
                new_tc.parts = (
                    new_tc.parts[:chunk] + [subst] + new_tc.parts[(chunk + 1) :]
                )
            maybe_moved_arguments += len(arg_defs)

            for test in iterator.try_testcase(new_tc, "Removing " + description):
                yield maybe_moved_arguments, test
                if iterator.last_feedback:
                    num_moved_arguments += maybe_moved_arguments
                    break
            else:
                num_survived_arguments += maybe_moved_arguments

            for arg_use in args_map["uses"]:
                chunk = arg_use["chunk"]
                values = arg_use["values"]
                if chunk == def_chunk and values == arg_defs:
                    continue

                new_tc = iterator.testcase.copy()
                subst = new_tc.parts[chunk].replace(arg_use["pattern"], fun + b"()", 1)
                if new_tc.parts[chunk] == subst:
                    continue
                new_tc.parts = (
                    new_tc.parts[:chunk] + [subst] + new_tc.parts[(chunk + 1) :]
                )
                maybe_moved_arguments = len(values)

                for test in iterator.try_testcase(
                    new_tc,
                    "Removing %s at %s #%d"
                    % (description, iterator.testcase.atom, chunk),
                ):
                    yield maybe_moved_arguments, test
                    if iterator.last_feedback:
                        num_moved_arguments += maybe_moved_arguments
                        break
                else:
                    num_survived_arguments += maybe_moved_arguments

        # Remove immediate anonymous function calls.
        for anon in anonymous_queue:
            noop_changes = 0
            maybe_moved_arguments = 0
            new_tc = iterator.testcase.copy()

            arg_defs = anon["defs"]
            def_chunk = anon["chunk"]
            values = anon["use"]
            chunk = anon["use_chunk"]
            description = "arguments of anonymous function at #%s %s" % (
                iterator.testcase.atom,
                def_chunk,
            )
            # Remove arguments of the function.
            subst = new_tc.parts[def_chunk].replace(b",".join(arg_defs), b"", 1)
            if new_tc.parts[def_chunk] == subst:
                noop_changes += 1
            new_tc.parts = (
                new_tc.parts[:def_chunk] + [subst] + new_tc.parts[(def_chunk + 1) :]
            )

            # Replace arguments by their value in the scope of the function.
            while len(values) < len(arg_defs):
                values = values + [b"undefined"]
            setters = b"".join(
                b"var %s = %s;\n" % (a, v) for a, v in zip(arg_defs, values)
            )
            subst = new_tc.parts[def_chunk] + b"\n" + setters
            if new_tc.parts[def_chunk] == subst:
                noop_changes += 1
            new_tc.parts = (
                new_tc.parts[:def_chunk] + [subst] + new_tc.parts[(def_chunk + 1) :]
            )

            # Remove arguments of the anonymous function call.
            subst = new_tc.parts[chunk].replace(b",".join(anon["use"]), b"", 1)
            if new_tc.parts[chunk] == subst:
                noop_changes += 1
            new_tc.parts = new_tc.parts[:chunk] + [subst] + new_tc.parts[(chunk + 1) :]
            maybe_moved_arguments += len(values)

            if noop_changes == 3:
                continue

            for test in iterator.try_testcase(new_tc, "Removing " + description):
                yield maybe_moved_arguments, test
                if iterator.last_feedback:
                    num_moved_arguments += maybe_moved_arguments
                    break
            else:
                num_survived_arguments += maybe_moved_arguments

        LOG.info("")
        LOG.info("Done with this round!")
        LOG.info("%s moved;", quantity(num_moved_arguments, "argument"))
        LOG.info("%s survived.", quantity(num_survived_arguments, "argument"))


class CollapseEmptyBraces(Minimize):
    """Perform standard line based reduction but collapse empty braces at the end of
    each round. This ensures that empty braces are reduced in a single pass of the
    reduction strategy.

    Example:
        // Original
        function foo() {
        }

        // Post-processed
        function foo() { }
    """

    name = "minimize-collapse-brace"

    @staticmethod
    def _post_round_cb(iterator):
        """Collapse braces separated by whitespace
        Args:
            testcase (Testcase): Testcase to be reduced.
        Returns:
            bool: True if callback was performed successfully, False otherwise.
        """
        raw = b"".join(iterator.testcase.parts)
        modified = re.sub(br"{\s+}", b"{ }", raw)

        # Don't update the testcase if no changes were applied
        if raw != modified:
            with open(iterator.testcase.filename, "wb") as testf:
                testf.write(iterator.testcase.before)
                testf.write(modified)
                testf.write(iterator.testcase.after)

            # Re-parse the modified testcase
            new_tc = iterator.testcase.copy()
            new_tc.load(iterator.testcase.filename)

            yield from iterator.try_testcase(new_tc, "Collapse empty braces")
