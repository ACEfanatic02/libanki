# -*- coding: utf-8 -*-
# Copyright: Damien Elmes <anki@ichi2.net>
# License: GNU GPL, version 3 or later; http://www.gnu.org/copyleft/gpl.html

import time, datetime, simplejson
from heapq import *
from anki.db import *
from anki.cards import Card
from anki.utils import parseTags
from anki.lang import _

# the standard Anki scheduler
class Scheduler(object):
    def __init__(self, deck):
        self.deck = deck
        self.db = deck.db
        self.name = "main"
        self.queueLimit = 200
        self.learnLimit = 1000
        self.updateCutoff()
        # restore any cards temporarily suspended by alternate schedulers
        try:
            self.resetSchedBuried()
        except OperationalError, e:
            # will fail if deck hasn't been upgraded yet
            print "resetSched() failed"

    def getCard(self, orm=True):
        "Pop the next card from the queue. None if finished."
        self.checkDay()
        id = self.getCardId()
        if id:
            card = Card()
            assert card.fromDB(self.db, id)
            return card

    def reset(self):
        self.modelConfigs = {}
        self.resetLearn()
        self.resetReview()
        self.resetNew()

    def answerCard(self, card, ease):
        if card.queue == 0:
            self.answerLearnCard(card, ease)
        elif card.queue == 1:
            self.answerRevCard(card, ease)
        else:
            raise Exception("Invalid queue")
        card.toDB(self.db)

    def counts(self):
        # FIXME: should learn count include new cards due today, or be separate?
        return (self.learnCount, self.revCount)

    def cardQueue(self, card):
        return card.queue

    # Getting the next card
    ##########################################################################

    def getCardId(self):
        "Return the next due card id, or None."
        # learning card due?
        id = self.getLearnCard()
        if id:
            return id
        # new first, or time for one?
        if self.timeForNewCard():
            return self.getNewCard()
        # card due for review?
        id = self.getReviewCard()
        if id:
            return id
        # new cards left?
        id = self.getNewCard()
        if id:
            return id
        # collapse or finish
        return self.getLearnCard(collapse=True)

    # Learning queue
    ##########################################################################

    def resetLearn(self):
        self.learnQueue = self.db.all("""
select due, id from cards where
queue = 0 and due < :lim order by due
limit %d""" % self.learnLimit, lim=self.dayCutoff)
        self.learnCount = len(self.learnQueue)

    def getLearnCard(self, collapse=False):
        if self.learnQueue:
            cutoff = time.time()
            if collapse:
                cutoff -= self.deck.collapseTime
            if self.learnQueue[0][0] < cutoff:
                return heappop(self.learnQueue)[1]

    def answerLearnCard(self, card, ease):
        # ease 1=no, 2=yes, 3=remove
        conf = self.learnConf(card)
        if ease == 3:
            self.removeLearnCard(card, conf)
            return
        card.cycles += 1
        if ease == 2:
            card.grade += 1
        else:
            card.grade = 0
        if card.grade >= len(conf['delays']):
            self.graduateLearnCard(card, conf)
        else:
            card.due = time.time() + conf['delays'][card.grade]*60

    def learnConf(self, card):
        conf = self.configForCard(card)
        if card.type == 2:
            return conf['new']
        else:
            return conf['lapse']

    def removeLearnCard(self, card, conf):
        if card.type == 1:
            int_ = None
        elif not card.cycles:
            # first time bonus
            int_ = conf['ints'][1]
        else:
            # normal remove
            int_ = conf['ints'][2]
        self.rescheduleAsReview(card, int_)

    def graduateLearnCard(self, card, conf):
        if card.type == 1:
            int_ = None
        else:
            int_ = conf['ints'][0]
        self.rescheduleAsReview(card, int_)

    def rescheduleAsReview(self, card, int_):
        card.queue = 1
        if int_:
            # new card
            card.type = 1
            card.interval = int_
            print "handle log, etc"

    # Reviews
    ##########################################################################

    def resetReview(self):
        self.revCount = self.db.scalar(
            self.cardLimit(
                "revActive", "revInactive",
                "select count(*) from cards c where queue = 1 "
                "and due < :lim"), lim=self.dayCutoff)
        self.revQueue = []

    def getReviewCard(self):
        if self.haveRevCards():
            return self.revQueue.pop()

    def haveRevCards(self):
        if self.revCount:
            if not self.revQueue:
                self.fillRevQueue()
            return self.revQueue

    def fillRevQueue(self):
        self.revQueue = self.db.all(
            self.cardLimit(
                "revActive", "revInactive", """
select c.id, factId from cards c where
queue = 1 and due < :lim order by %s
limit %d""" % (self.revOrder(), self.queueLimit)), lim=self.dayCutoff)
        self.revQueue.reverse()

    # FIXME: current random order won't work with new spacing
    def revOrder(self):
        return ("interval desc",
                "interval",
                "due",
                "factId, ordinal")[self.revCardOrder]

    # FIXME: rewrite
    def showFailedLast(self):
        return self.collapseTime or not self.delay0

    # Answering a card
    ##########################################################################

    def _answerCard(self, card, ease):
        undoName = _("Answer Card")
        self.setUndoStart(undoName)
        now = time.time()
        # old state
        oldState = self.cardState(card)
        oldQueue = self.cardQueue(card)
        lastDelaySecs = time.time() - card.due
        lastDelay = lastDelaySecs / 86400.0
        oldSuc = card.successive
        # update card details
        last = card.interval
        card.interval = self.nextInterval(card, ease)
        card.lastInterval = last
        if card.reps:
            # only update if card was not new
            card.lastDue = card.due
        card.due = self.nextDue(card, ease, oldState)
        if not self.finishScheduler:
            # don't update factor in custom schedulers
            self.updateFactor(card, ease)
        # spacing
        self.spaceCards(card)
        # adjust counts for current card
        if ease == 1:
            if card.due < self.dayCutoff:
                self.learnCount += 1
        if oldQueue == 0:
            self.learnCount -= 1
        elif oldQueue == 1:
            self.revCount -= 1
        else:
            self.newAvail -= 1
        # card stats
        self.updateCardStats(card, ease, oldState)
        # update type & ensure past cutoff
        card.type = self.cardType(card)
        card.queue = card.type
        if ease != 1:
            card.due = max(card.due, self.dayCutoff+1)
        # allow custom schedulers to munge the card
        if self.answerPreSave:
            self.answerPreSave(card, ease)
        # save
        card.due = card.due
        card.toDB(self.db)
        # review history
        print "make sure flags is set correctly when reviewing early"
        logReview(self.db, card, ease, 0)
        self.modified = now
        # leech handling - we need to do this after the queue, as it may cause
        # a reset()
        isLeech = self.isLeech(card)
        if isLeech:
            self.handleLeech(card)
        runHook("cardAnswered", card.id, isLeech)
        self.setUndoEnd(undoName)

    def updateCardStats(self, card, ease, state):
        card.reps += 1
        if ease == 1:
            card.successive = 0
            card.lapses += 1
        else:
            card.successive += 1
        # if not card.firstAnswered:
        #     card.firstAnswered = time.time()
        card.setModified()

    def spaceCards(self, card):
        new = time.time() + self.newSpacing
        self.db.statement("""
update cards set
due = (case
when queue = 1 then due + 86400 * (case
  when interval*:rev < 1 then 0
  else interval*:rev
  end)
when queue = 2 then :new
end),
modified = :now
where id != :id and factId = :factId
and due < :cut
and queue between 1 and 2""",
                         id=card.id, now=time.time(), factId=card.factId,
                         cut=self.dayCutoff, new=new, rev=self.revSpacing)
        # update local cache of seen facts
        self.spacedFacts[card.factId] = new

    # Interval management
    ##########################################################################

    def nextInterval(self, card, ease):
        "Return the next interval for CARD given EASE."
        delay = self.adjustedDelay(card, ease)
        return self._nextInterval(card, delay, ease)

    def _nextInterval(self, card, delay, ease):
        interval = card.interval
        factor = card.factor
        # if cramming / reviewing early
        if delay < 0:
            interval = max(card.lastInterval, card.interval + delay)
            if interval < self.midIntervalMin:
                interval = 0
            delay = 0
        # if interval is less than mid interval, use presets
        if ease == 1:
            interval *= self.delay2
            if interval < self.hardIntervalMin:
                interval = 0
        elif interval == 0:
            if ease == 2:
                interval = random.uniform(self.hardIntervalMin,
                                          self.hardIntervalMax)
            elif ease == 3:
                interval = random.uniform(self.midIntervalMin,
                                          self.midIntervalMax)
            elif ease == 4:
                interval = random.uniform(self.easyIntervalMin,
                                          self.easyIntervalMax)
        else:
            # if not cramming, boost initial 2
            if (interval < self.hardIntervalMax and
                interval > 0.166):
                mid = (self.midIntervalMin + self.midIntervalMax) / 2.0
                interval = mid / factor
            # multiply last interval by factor
            if ease == 2:
                interval = (interval + delay/4) * 1.2
            elif ease == 3:
                interval = (interval + delay/2) * factor
            elif ease == 4:
                interval = (interval + delay) * factor * self.factorFour
            fuzz = random.uniform(0.95, 1.05)
            interval *= fuzz
        return interval

    def nextIntervalStr(self, card, ease, short=False):
        "Return the next interval for CARD given EASE as a string."
        int = self.nextInterval(card, ease)
        return anki.utils.fmtTimeSpan(int*86400, short=short)

    def nextDue(self, card, ease, oldState):
        "Return time when CARD will expire given EASE."
        if ease == 1:
            # 600 is a magic value which means no bonus, and is used to ease
            # upgrades
            cram = self.scheduler == "cram"
            if (not cram and oldState == "mature"
                and self.delay1 and self.delay1 != 600):
                # user wants a bonus of 1+ days. put the failed cards at the
                # start of the future day, so that failures that day will come
                # after the waiting cards
                return self.dayCutoff + (self.delay1 - 1)*86400
            else:
                due = 0
        else:
            due = card.interval * 86400.0
        return due + time.time()

    def updateFactor(self, card, ease):
        "Update CARD's factor based on EASE."
        print "update cardIsBeingLearnt()"
        if not card.reps:
            # card is new, inherit beginning factor
            card.factor = self.averageFactor
        if card.successive and not self.cardIsBeingLearnt(card):
            if ease == 1:
                card.factor -= 0.20
            elif ease == 2:
                card.factor -= 0.15
        if ease == 4:
            card.factor += 0.10
        card.factor = max(1.3, card.factor)

    def adjustedDelay(self, card, ease):
        "Return an adjusted delay value for CARD based on EASE."
        if self.cardIsNew(card):
            return 0
        if card.due <= self.dayCutoff:
            return (self.dayCutoff - card.due) / 86400.0
        else:
            return (self.dayCutoff - card.due) / 86400.0

    # Leeches
    ##########################################################################

    def isLeech(self, card):
        no = card.lapses
        fmax = self.getInt('leechFails')
        if not fmax:
            return
        return (
            # failed
            not card.successive and
            # greater than fail threshold
            no >= fmax and
            # at least threshold/2 reps since last time
            (fmax - no) % (max(fmax/2, 1)) == 0)

    def handleLeech(self, card):
        self.refreshSession()
        scard = self.cardFromId(card.id, True)
        tags = scard.fact.tags
        tags = addTags("Leech", tags)
        scard.fact.tags = canonifyTags(tags)
        scard.fact.setModified(textChanged=True, deck=self)
        self.updateFactTags([scard.fact.id])
        self.db.flush()
        self.db.expunge(scard)
        if self.getBool('suspendLeeches'):
            self.suspendCards([card.id])
        self.reset()
        self.refreshSession()

    # New cards
    ##########################################################################

#         # day counts
#         (self.repsToday, self.newSeenToday) = self.db.first("""
# select count(), sum(case when rep = 1 then 1 else 0 end) from revlog
# where time > :t""", t=self.dayCutoff-86400)
#         self.newSeenToday = self.newSeenToday or 0
#         print "newSeenToday in answer(), reset called twice"
#         print "newSeenToday needs to account for drill mode too."

    # when do we do this?
    #self.updateNewCountToday()

    def resetNew(self):
#        self.updateNewCardRatio()
        pass

    def rebuildNewCount(self):
        self.newAvail = self.db.scalar(
            self.cardLimit(
            "newActive", "newInactive",
            "select count(*) from cards c where queue = 2 "
            "and due < :lim"), lim=self.dayCutoff)
        self.updateNewCountToday()

    def updateNewCountToday(self):
        self.newCount = max(min(
            self.newAvail, self.newCardsPerDay -
            self.newSeenToday), 0)

    def fillNewQueue(self):
        if self.newCount and not self.newQueue:
            self.newQueue = self.db.all(
                self.cardLimit(
                "newActive", "newInactive", """
select c.id, factId from cards c where
queue = 2 and due < :lim order by %s
limit %d""" % (self.newOrder(), self.queueLimit)), lim=self.dayCutoff)
            self.newQueue.reverse()

    def updateNewCardRatio(self):
        if self.newCardSpacing == NEW_CARDS_DISTRIBUTE:
            if self.newCount:
                self.newCardModulus = (
                    (self.newCount + self.revCount) / self.newCount)
                # if there are cards to review, ensure modulo >= 2
                if self.revCount:
                    self.newCardModulus = max(2, self.newCardModulus)
            else:
                self.newCardModulus = 0
        else:
            self.newCardModulus = 0

    def timeForNewCard(self):
        "True if it's time to display a new card when distributing."
        # FIXME
        return False

        if not self.newCount:
            return False
        if self.newCardSpacing == NEW_CARDS_LAST:
            return False
        if self.newCardSpacing == NEW_CARDS_FIRST:
            return True
        if self.newCardModulus:
            return self.repsToday % self.newCardModulus == 0
        else:
            return False

    def getNewCard(self):
        # FIXME
        return None
        #return self.newQueue[-1][0]

    def newOrder(self):
        return ("due",
                "due",
                "due desc")[self.newCardOrder]

    # Tools
    ##########################################################################

    def configForCard(self, card):
        mid = card.modelId
        if not mid in self.modelConfigs:
            self.modelConfigs[mid] = simplejson.loads(
                self.db.scalar("select config from models where id = :id",
                               id=mid))
        return self.modelConfigs[mid]

    def resetSchedBuried(self):
        "Put temporarily suspended cards back into play."
        self.db.statement(
            "update cards set queue = type where queue = -3")

    def cardLimit(self, active, inactive, sql):
        yes = parseTags(getattr(self.deck, active))
        no = parseTags(getattr(self.deck, inactive))
        if yes:
            yids = tagIds(self.db, yes).values()
            nids = tagIds(self.db, no).values()
            return sql.replace(
                "where",
                "where +c.id in (select cardId from cardTags where "
                "tagId in %s) and +c.id not in (select cardId from "
                "cardTags where tagId in %s) and" % (
                ids2str(yids),
                ids2str(nids)))
        elif no:
            nids = tagIds(self.db, no).values()
            return sql.replace(
                "where",
                "where +c.id not in (select cardId from cardTags where "
                "tagId in %s) and" % ids2str(nids))
        else:
            return sql

    # Daily cutoff
    ##########################################################################

    def updateCutoff(self):
        d = datetime.datetime.utcfromtimestamp(
            time.time() - self.deck.utcOffset) + datetime.timedelta(days=1)
        d = datetime.datetime(d.year, d.month, d.day)
        newday = self.deck.utcOffset - time.timezone
        d += datetime.timedelta(seconds=newday)
        cutoff = time.mktime(d.timetuple())
        # cutoff must not be in the past
        while cutoff < time.time():
            cutoff += 86400
        # cutoff must not be more than 24 hours in the future
        cutoff = min(time.time() + 86400, cutoff)
        self.dayCutoff = cutoff
        self.dayCount = int(cutoff/86400 - self.deck.created/86400)

    def checkDay(self):
        # check if the day has rolled over
        if time.time() > self.dayCutoff:
            self.updateCutoff()
            self.reset()

    # Review early
    ##########################################################################

    def setupReviewEarlyScheduler(self):
        self.fillRevQueue = self._fillRevEarlyQueue
        self.rebuildRevCount = self._rebuildRevEarlyCount
        self.finishScheduler = self.setupStandardScheduler
        self.answerPreSave = self._reviewEarlyPreSave
        self.scheduler = "reviewEarly"

    def _reviewEarlyPreSave(self, card, ease):
        if ease > 1:
            # prevent it from appearing in next queue fill
            card.queue = -3

    def _rebuildRevEarlyCount(self):
        # in the future it would be nice to skip the first x days of due cards
        self.revCount = self.db.scalar(
            self.cardLimit(
            "revActive", "revInactive", """
select count() from cards c where queue = 1 and due > :now
"""), now=self.dayCutoff)

    def _fillRevEarlyQueue(self):
        if self.revCount and not self.revQueue:
            self.revQueue = self.db.all(
                self.cardLimit(
                "revActive", "revInactive", """
select id, factId from cards c where queue = 1 and due > :lim
order by due limit %d""" % self.queueLimit), lim=self.dayCutoff)
            self.revQueue.reverse()

    # Learn more
    ##########################################################################

    def setupLearnMoreScheduler(self):
        self.rebuildNewCount = self._rebuildLearnMoreCount
        self.updateNewCountToday = self._updateLearnMoreCountToday
        self.finishScheduler = self.setupStandardScheduler
        self.scheduler = "learnMore"

    def _rebuildLearnMoreCount(self):
        self.newAvail = self.db.scalar(
            self.cardLimit(
            "newActive", "newInactive",
            "select count(*) from cards c where queue = 2 "
            "and due < :lim"), lim=self.dayCutoff)
        self.spacedCards = []

    def _updateLearnMoreCountToday(self):
        self.newCount = self.newAvail
