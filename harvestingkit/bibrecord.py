# -*- coding: utf-8 -*-
#
# This file is part of Harvesting Kit.
# Copyright (C) 2015 CERN.
#
# Harvesting Kit is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License as
# published by the Free Software Foundation; either version 2 of the
# License, or (at your option) any later version.
#
# Harvesting Kit is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Harvesting Kit; if not, write to the Free Software Foundation, Inc.,
# 59 Temple Place, Suite 330, Boston, MA 02111-1307, USA.

"""
BibRecord - XML MARC processing library for Invenio adapted for Harvesting Kit.

The main usage of BibRecord in Harvesting Kit is linked to MARCXML conversions:

    >>> from harvestingkit.bibrecord import BibRecordPackage
    >>> from harvestingkit.inspire_cds_package.from_inspire import Inspire2CDS
    >>> bibrecs = BibRecordPackage("inspire.xml")
    >>> xml = Inspire2CDS.convert(bibrecs)

"""

import re
import sys
import os

from lxml import etree
from six import StringIO
from xml.etree import ElementTree as ET

from .utils import (
    escape_for_xml,
    create_logger,
)
from .etree_utils import (
    element_tree_oai_records,
    element_tree_collection_to_records,
    strip_xml_namespace,
    get_request_subfields,
)

if sys.hexversion < 0x2040000:
    # pylint: disable=W0622
    from sets import Set as set
    # pylint: enable=W0622

# Do we remove singletons (empty tags)?
# NOTE: this is currently set to True as there are some external workflow
# exploiting singletons, e.g. bibupload -c used to delete fields, and
# bibdocfile --fix-marc called on a record where the latest document
# has been deleted.
CFG_BIBRECORD_KEEP_SINGLETONS = True

# internal dictionary of warning messages:
CFG_BIBRECORD_WARNING_MSGS = {
    0: "",
    1: "WARNING: tag missing for field(s)\nValue stored with tag '000'",
    2: "WARNING: bad range for tags (tag must be in range 001-999)\nValue stored with tag '000'",
    3: "WARNING: Missing atribute 'code' for subfield\nValue stored with code ''",
    4: "WARNING: Missing attribute 'ind1'\nValue stored with ind1 = ''",
    5: "WARNING: Missing attribute 'ind2'\nValue stored with ind2 = ''",
    6: "Import Error",
    7: "WARNING: value expected of type string.",
    8: "WARNING: empty datafield",
    98: "WARNING: problems importing invenio",
    99: "Document not well formed",
    }

# verbose level to be used when creating records from XML: (0=least, ..., 9=most)
CFG_BIBRECORD_DEFAULT_VERBOSE_LEVEL = 0

# correction level to be used when creating records from XML: (0=no, 1=yes)
CFG_BIBRECORD_DEFAULT_CORRECT = 0

CFG_BIBUPLOAD_EXTERNAL_OAIID_TAG = "035__a"


class InvenioBibRecordParserError(Exception):

    """A generic parsing exception for all available parsers."""


class InvenioBibRecordFieldError(Exception):

    """An generic error for BibRecord."""


class BibRecordPackage(object):

    """Parsing a MARCXML document from to BibRecords.

    NOTE: It also supports OAI-PMH MARCXML.
    """

    def __init__(self, path=None):
        """Given a path to a MARCXML file, create an object to parse & convert it.

        :param path: the actual path of a Invenio style MARCXML.
        """
        self.path = path
        self.records = []
        self.deleted_records = []
        self.logger = create_logger("BibRecord")

    def parse(self, path_to_xml=None):
        """Parse an XML document and clean any namespaces."""
        if not path_to_xml:
            if not self.path:
                self.logger.error("No path defined!")
                return
            path_to_xml = self.path
        root = self._clean_xml(path_to_xml)

        # See first of this XML is clean or OAI request
        if root.tag.lower() == 'collection':
            tree = ET.ElementTree(root)
            self.records = element_tree_collection_to_records(tree)
        elif root.tag.lower() == 'record':
            new_root = ET.Element('collection')
            new_root.append(root)
            tree = ET.ElementTree(new_root)
            self.records = element_tree_collection_to_records(tree)
        else:
            # We have an OAI request
            header_subs = get_request_subfields(root)
            records = root.find('ListRecords')
            if records is None:
                records = root.find('GetRecord')
            if records is None:
                raise ValueError("Cannot find ListRecords or GetRecord!")

            tree = ET.ElementTree(records)
            for record, is_deleted in element_tree_oai_records(tree, header_subs):
                if is_deleted:
                    # It was OAI deleted. Create special record
                    self.deleted_records.append(
                        self.create_deleted_record(record)
                    )
                else:
                    self.records.append(record)

    def _clean_xml(self, path_to_xml):
        """Clean MARCXML harvested from OAI.

        Allows the xml to be used with BibUpload or BibRecord.

        :param xml: either XML as a string or path to an XML file

        :return: ElementTree of clean data
        """
        try:
            if os.path.isfile(path_to_xml):
                tree = ET.parse(path_to_xml)
            else:
                self.logger.warning(
                    "input is not a valid file," +
                    " attempting to parse input as XML..."
                )
                tree = ET.fromstring(path_to_xml)
        except Exception, e:
            self.logger.error("Could not read OAI XML, aborting filter!")
            raise e
        root = tree.getroot()
        strip_xml_namespace(root)
        return root

    def create_deleted_record(self, record):
        """Generate the record deletion if deleted form OAI-PMH."""
        identifier = record_get_field_value(record,
                                            tag="037",
                                            code="a")
        recid = identifier.split(":")[-1]
        try:
            source = identifier.split(":")[1]
        except IndexError:
            source = "Unknown"
        record_add_field(record, "035",
                         subfields=[("9", source), ("a", recid)])
        record_add_field(record, "980",
                         subfields=[("c", "DELETED")])
        return record

    def get_records(self):
        """Return all records found."""
        return self.records


def create_field(subfields=None, ind1=' ', ind2=' ', controlfield_value='',
                 global_position=-1):
    """
    Return a field created with the provided elements.

    Global position is set arbitrary to -1.
    """
    if subfields is None:
        subfields = []

    ind1, ind2 = _wash_indicators(ind1, ind2)
    field = (subfields, ind1, ind2, controlfield_value, global_position)
    _check_field_validity(field)
    return field


def create_records(marcxml, verbose=CFG_BIBRECORD_DEFAULT_VERBOSE_LEVEL,
                   correct=CFG_BIBRECORD_DEFAULT_CORRECT, parser='',
                   keep_singletons=CFG_BIBRECORD_KEEP_SINGLETONS):
    """
    Create a list of records from the marcxml description.

    :returns: a list of objects initiated by the function create_record().
              Please see that function's docstring.
    """
    # Use the DOTALL flag to include newlines.
    regex = re.compile('<record.*?>.*?</record>', re.DOTALL)
    record_xmls = regex.findall(marcxml)

    return [create_record(record_xml, verbose=verbose, correct=correct,
            parser=parser, keep_singletons=keep_singletons)
            for record_xml in record_xmls]


def create_record(marcxml, verbose=CFG_BIBRECORD_DEFAULT_VERBOSE_LEVEL,
                  correct=CFG_BIBRECORD_DEFAULT_CORRECT, parser='',
                  sort_fields_by_indicators=False,
                  keep_singletons=CFG_BIBRECORD_KEEP_SINGLETONS):
    """Create a record object from the marcxml description.

    Uses the lxml parser.

    The returned object is a tuple (record, status_code, list_of_errors),
    where status_code is 0 when there are errors, 1 when no errors.

    The return record structure is as follows::

        Record := {tag : [Field]}
        Field := (Subfields, ind1, ind2, value)
        Subfields := [(code, value)]

    .. code-block:: none

                                    .--------.
                                    | record |
                                    '---+----'
                                        |
               .------------------------+------------------------------------.
               |record['001']           |record['909']        |record['520'] |
               |                        |                     |              |
        [list of fields]           [list of fields]     [list of fields]    ...
               |                        |                     |
               |               .--------+--+-----------.      |
               |               |           |           |      |
               |[0]            |[0]        |[1]       ...     |[0]
          .----+------.  .-----+-----.  .--+--------.     .---+-------.
          | Field 001 |  | Field 909 |  | Field 909 |     | Field 520 |
          '-----------'  '-----+-----'  '--+--------'     '---+-------'
               |               |           |                  |
              ...              |          ...                ...
                               |
                    .----------+-+--------+------------.
                    |            |        |            |
                    |[0]         |[1]     |[2]         |
          [list of subfields]   'C'      '4'          ...
                    |
               .----+---------------+------------------------+
               |                    |                        |
        ('a', 'value')              |            ('a', 'value for another a')
                     ('b', 'value for subfield b')

    :param marcxml: an XML string representation of the record to create
    :param verbose: the level of verbosity: 0 (silent), 1-2 (warnings),
                    3(strict:stop when errors)
    :param correct: 1 to enable correction of marcxml syntax. Else 0.
    :return: a tuple (record, status_code, list_of_errors), where status
             code is 0 where there are errors, 1 when no errors
    """
    try:
        rec = _create_record_lxml(marcxml, verbose, correct,
                                  keep_singletons=keep_singletons)
    except InvenioBibRecordParserError as ex1:
        return (None, 0, str(ex1))

    if sort_fields_by_indicators:
        _record_sort_by_indicators(rec)

    errs = []
    if correct:
        # Correct the structure of the record.
        errs = _correct_record(rec)

    return (rec, int(not errs), errs)


def filter_field_instances(field_instances, filter_subcode, filter_value,
                           filter_mode='e'):
    """Filter the given field.

    Filters given field and returns only that field instances that contain
    filter_subcode with given filter_value. As an input for search function
    accepts output from record_get_field_instances function. Function can be
    run in three modes:

    - 'e' - looking for exact match in subfield value
    - 's' - looking for substring in subfield value
    - 'r' - looking for regular expression in subfield value

    Example:

    record_filter_field(record_get_field_instances(rec, '999', '%', '%'),
                        'y', '2001')

    In this case filter_subcode is 'y' and filter_value is '2001'.

    :param field_instances: output from record_get_field_instances
    :param filter_subcode: name of the subfield
    :type filter_subcode: string
    :param filter_value: value of the subfield
    :type filter_value: string
    :param filter_mode: 'e','s' or 'r'
    """
    matched = []
    if filter_mode == 'e':
        to_match = (filter_subcode, filter_value)
        for instance in field_instances:
            if to_match in instance[0]:
                matched.append(instance)
    elif filter_mode == 's':
        for instance in field_instances:
            for subfield in instance[0]:
                if subfield[0] == filter_subcode and \
                   subfield[1].find(filter_value) > -1:
                    matched.append(instance)
                    break
    elif filter_mode == 'r':
        reg_exp = re.compile(filter_value)
        for instance in field_instances:
            for subfield in instance[0]:
                if subfield[0] == filter_subcode and \
                   reg_exp.match(subfield[1]) is not None:
                    matched.append(instance)
                    break
    return matched


def record_drop_duplicate_fields(record):
    """
    Return a record where all the duplicate fields have been removed.

    Fields are considered identical considering also the order of their
    subfields.
    """
    out = {}
    position = 0
    tags = sorted(record.keys())
    for tag in tags:
        fields = record[tag]
        out[tag] = []
        current_fields = set()
        for full_field in fields:
            field = (tuple(full_field[0]),) + full_field[1:4]
            if field not in current_fields:
                current_fields.add(field)
                position += 1
                out[tag].append(full_field[:4] + (position,))
    return out


def records_identical(rec1, rec2, skip_005=True, ignore_field_order=False,
                      ignore_subfield_order=False,
                      ignore_duplicate_subfields=False,
                      ignore_duplicate_controlfields=False):
    """
    Return True if rec1 is identical to rec2.

    It does so regardless of a difference in the 005 tag (i.e. the timestamp).
    """
    rec1_keys = set(rec1.keys())
    rec2_keys = set(rec2.keys())
    if skip_005:
        rec1_keys.discard("005")
        rec2_keys.discard("005")
    if rec1_keys != rec2_keys:
        return False
    for key in rec1_keys:
        if ignore_duplicate_controlfields and key.startswith('00'):
            if set(field[3] for field in rec1[key]) != \
                    set(field[3] for field in rec2[key]):
                return False
            continue

        rec1_fields = rec1[key]
        rec2_fields = rec2[key]
        if len(rec1_fields) != len(rec2_fields):
            # They already differs in length...
            return False
        if ignore_field_order:
            # We sort the fields, first by indicators and then by anything else
            rec1_fields = sorted(
                rec1_fields,
                key=lambda elem: (elem[1], elem[2], elem[3], elem[0]))
            rec2_fields = sorted(
                rec2_fields,
                key=lambda elem: (elem[1], elem[2], elem[3], elem[0]))
        else:
            # We sort the fields, first by indicators, then by global position
            # and then by anything else
            rec1_fields = sorted(
                rec1_fields,
                key=lambda elem: (elem[1], elem[2], elem[4], elem[3], elem[0]))
            rec2_fields = sorted(
                rec2_fields,
                key=lambda elem: (elem[1], elem[2], elem[4], elem[3], elem[0]))
        for field1, field2 in zip(rec1_fields, rec2_fields):
            if ignore_duplicate_subfields:
                if field1[1:4] != field2[1:4] or \
                        set(field1[0]) != set(field2[0]):
                    return False
            elif ignore_subfield_order:
                if field1[1:4] != field2[1:4] or \
                        sorted(field1[0]) != sorted(field2[0]):
                    return False
            elif field1[:4] != field2[:4]:
                return False
    return True


def record_get_field_instances(rec, tag="", ind1=" ", ind2=" "):
    """
    Return the list of field instances for the specified tag and indications.

    Return empty list if not found.
    If tag is empty string, returns all fields

    Parameters (tag, ind1, ind2) can contain wildcard %.

    :param rec: a record structure as returned by create_record()
    :param tag: a 3 characters long string
    :param ind1: a 1 character long string
    :param ind2: a 1 character long string
    :param code: a 1 character long string
    :return: a list of field tuples (Subfields, ind1, ind2, value,
             field_position_global) where subfields is list of (code, value)
    """
    if not rec:
        return []
    if not tag:
        return rec.items()
    else:
        out = []
        ind1, ind2 = _wash_indicators(ind1, ind2)

        if '%' in tag:
            # Wildcard in tag. Check all possible
            for field_tag in rec:
                if _tag_matches_pattern(field_tag, tag):
                    for possible_field_instance in rec[field_tag]:
                        if (ind1 in ('%', possible_field_instance[1]) and
                                ind2 in ('%', possible_field_instance[2])):
                            out.append(possible_field_instance)
        else:
            # Completely defined tag. Use dict
            for possible_field_instance in rec.get(tag, []):
                if (ind1 in ('%', possible_field_instance[1]) and
                        ind2 in ('%', possible_field_instance[2])):
                    out.append(possible_field_instance)
        return out


def record_add_field(rec, tag, ind1=' ', ind2=' ', controlfield_value='',
                     subfields=None, field_position_global=None,
                     field_position_local=None):
    """
    Add a new field into the record.

    If field_position_global or field_position_local is specified then
    this method will insert the new field at the desired position.
    Otherwise a global field position will be computed in order to
    insert the field at the best position (first we try to keep the
    order of the tags and then we insert the field at the end of the
    fields with the same tag).

    If both field_position_global and field_position_local are present,
    then field_position_local takes precedence.

    :param rec: the record data structure
    :param tag: the tag of the field to be added
    :param ind1: the first indicator
    :param ind2: the second indicator
    :param controlfield_value: the value of the controlfield
    :param subfields: the subfields (a list of tuples (code, value))
    :param field_position_global: the global field position (record wise)
    :param field_position_local: the local field position (tag wise)
    :return: the global field position of the newly inserted field or -1 if the
             operation failed
    """
    error = _validate_record_field_positions_global(rec)
    if error:
        # FIXME one should write a message here
        pass

    # Clean the parameters.
    if subfields is None:
        subfields = []
    ind1, ind2 = _wash_indicators(ind1, ind2)

    if controlfield_value and (ind1 != ' ' or ind2 != ' ' or subfields):
        return -1

    # Detect field number to be used for insertion:
    # Dictionaries for uniqueness.
    tag_field_positions_global = {}.fromkeys([field[4]
                                              for field in rec.get(tag, [])])
    all_field_positions_global = {}.fromkeys([field[4]
                                              for fields in rec.values()
                                              for field in fields])

    if field_position_global is None and field_position_local is None:
        # Let's determine the global field position of the new field.
        if tag in rec:
            try:
                field_position_global = max([field[4] for field in rec[tag]]) \
                    + 1
            except IndexError:
                if tag_field_positions_global:
                    field_position_global = max(tag_field_positions_global) + 1
                elif all_field_positions_global:
                    field_position_global = max(all_field_positions_global) + 1
                else:
                    field_position_global = 1
        else:
            if tag in ('FMT', 'FFT', 'BDR', 'BDM'):
                # Add the new tag to the end of the record.
                if tag_field_positions_global:
                    field_position_global = max(tag_field_positions_global) + 1
                elif all_field_positions_global:
                    field_position_global = max(all_field_positions_global) + 1
                else:
                    field_position_global = 1
            else:
                # Insert the tag in an ordered way by selecting the
                # right global field position.
                immediate_lower_tag = '000'
                for rec_tag in rec:
                    if (tag not in ('FMT', 'FFT', 'BDR', 'BDM') and
                            immediate_lower_tag < rec_tag < tag):
                        immediate_lower_tag = rec_tag

                if immediate_lower_tag == '000':
                    field_position_global = 1
                else:
                    field_position_global = rec[immediate_lower_tag][-1][4] + 1

        field_position_local = len(rec.get(tag, []))
        _shift_field_positions_global(rec, field_position_global, 1)
    elif field_position_local is not None:
        if tag in rec:
            if field_position_local >= len(rec[tag]):
                field_position_global = rec[tag][-1][4] + 1
            else:
                field_position_global = rec[tag][field_position_local][4]
            _shift_field_positions_global(rec, field_position_global, 1)
        else:
            if all_field_positions_global:
                field_position_global = max(all_field_positions_global) + 1
            else:
                # Empty record.
                field_position_global = 1
    elif field_position_global is not None:
        # If the user chose an existing global field position, shift all the
        # global field positions greater than the input global field position.
        if tag not in rec:
            if all_field_positions_global:
                field_position_global = max(all_field_positions_global) + 1
            else:
                field_position_global = 1
            field_position_local = 0
        elif field_position_global < min(tag_field_positions_global):
            field_position_global = min(tag_field_positions_global)
            _shift_field_positions_global(rec, min(tag_field_positions_global),
                                          1)
            field_position_local = 0
        elif field_position_global > max(tag_field_positions_global):
            field_position_global = max(tag_field_positions_global) + 1
            _shift_field_positions_global(rec,
                                          max(tag_field_positions_global) + 1,
                                          1)
            field_position_local = len(rec.get(tag, []))
        else:
            if field_position_global in tag_field_positions_global:
                _shift_field_positions_global(rec, field_position_global, 1)

            field_position_local = 0
            for position, field in enumerate(rec[tag]):
                if field[4] == field_position_global + 1:
                    field_position_local = position

    # Create the new field.
    newfield = (subfields, ind1, ind2, str(controlfield_value),
                field_position_global)
    rec.setdefault(tag, []).insert(field_position_local, newfield)

    # Return new field number:
    return field_position_global


def record_has_field(rec, tag):
    """
    Check if the tag exists in the record.

    :param rec: the record data structure
    :param the: field
    :return: a boolean
    """
    return tag in rec


def record_delete_field(rec, tag, ind1=' ', ind2=' ',
                        field_position_global=None, field_position_local=None):
    """
    Delete the field with the given position.

    If global field position is specified, deletes the field with the
    corresponding global field position.
    If field_position_local is specified, deletes the field with the
    corresponding local field position and tag.
    Else deletes all the fields matching tag and optionally ind1 and
    ind2.

    If both field_position_global and field_position_local are present,
    then field_position_local takes precedence.

    :param rec: the record data structure
    :param tag: the tag of the field to be deleted
    :param ind1: the first indicator of the field to be deleted
    :param ind2: the second indicator of the field to be deleted
    :param field_position_global: the global field position (record wise)
    :param field_position_local: the local field position (tag wise)
    :return: the list of deleted fields
    """
    error = _validate_record_field_positions_global(rec)
    if error:
        # FIXME one should write a message here.
        pass

    if tag not in rec:
        return False

    ind1, ind2 = _wash_indicators(ind1, ind2)

    deleted = []
    newfields = []

    if field_position_global is None and field_position_local is None:
        # Remove all fields with tag 'tag'.
        for field in rec[tag]:
            if field[1] != ind1 or field[2] != ind2:
                newfields.append(field)
            else:
                deleted.append(field)
        rec[tag] = newfields
    elif field_position_global is not None:
        # Remove the field with 'field_position_global'.
        for field in rec[tag]:
            if (field[1] != ind1 and field[2] != ind2 or
                    field[4] != field_position_global):
                newfields.append(field)
            else:
                deleted.append(field)
        rec[tag] = newfields
    elif field_position_local is not None:
        # Remove the field with 'field_position_local'.
        try:
            del rec[tag][field_position_local]
        except IndexError:
            return []

    if not rec[tag]:
        # Tag is now empty, remove it.
        del rec[tag]

    return deleted


def record_delete_fields(rec, tag, field_positions_local=None):
    """
    Delete all/some fields defined with MARC tag 'tag' from record 'rec'.

    :param rec: a record structure.
    :type rec: tuple
    :param tag: three letter field.
    :type tag: string
    :param field_position_local: if set, it is the list of local positions
        within all the fields with the specified tag, that should be deleted.
        If not set all the fields with the specified tag will be deleted.
    :type field_position_local: sequence
    :return: the list of deleted fields.
    :rtype: list
    :note: the record is modified in place.
    """
    if tag not in rec:
        return []

    new_fields, deleted_fields = [], []

    for position, field in enumerate(rec.get(tag, [])):
        if field_positions_local is None or position in field_positions_local:
            deleted_fields.append(field)
        else:
            new_fields.append(field)

    if new_fields:
        rec[tag] = new_fields
    else:
        del rec[tag]

    return deleted_fields


def record_add_fields(rec, tag, fields, field_position_local=None,
                      field_position_global=None):
    """
    Add the fields into the record at the required position.

    The position is specified by the tag and the field_position_local in the
    list of fields.

    :param rec: a record structure
    :param tag: the tag of the fields to be moved
    :param field_position_local: the field_position_local to which the field
                                 will be inserted. If not specified, appends
                                 the fields to the tag.
    :param a: list of fields to be added
    :return: -1 if the operation failed, or the field_position_local if it was
             successful
    """
    if field_position_local is None and field_position_global is None:
        for field in fields:
            record_add_field(
                rec, tag, ind1=field[1],
                ind2=field[2], subfields=field[0],
                controlfield_value=field[3])
    else:
        fields.reverse()
        for field in fields:
            record_add_field(
                rec, tag, ind1=field[1], ind2=field[2],
                subfields=field[0], controlfield_value=field[3],
                field_position_local=field_position_local,
                field_position_global=field_position_global)

    return field_position_local


def record_move_fields(rec, tag, field_positions_local,
                       field_position_local=None):
    """
    Move some fields to the position specified by 'field_position_local'.

    :param rec: a record structure as returned by create_record()
    :param tag: the tag of the fields to be moved
    :param field_positions_local: the positions of the fields to move
    :param field_position_local: insert the field before that
                                 field_position_local. If unspecified, appends
                                 the fields :return: the field_position_local
                                 is the operation was successful
    """
    fields = record_delete_fields(
        rec, tag,
        field_positions_local=field_positions_local)
    return record_add_fields(
        rec, tag, fields,
        field_position_local=field_position_local)


def record_delete_subfield(rec, tag, subfield_code, ind1=' ', ind2=' '):
    """Delete all subfields with subfield_code in the record."""
    ind1, ind2 = _wash_indicators(ind1, ind2)

    for field in rec.get(tag, []):
        if field[1] == ind1 and field[2] == ind2:
            field[0][:] = [subfield for subfield in field[0]
                           if subfield_code != subfield[0]]


def record_get_field(rec, tag, field_position_global=None,
                     field_position_local=None):
    """
    Return the the matching field.

    One has to enter either a global field position or a local field position.

    :return: a list of subfield tuples (subfield code, value).
    :rtype: list
    """
    if field_position_global is None and field_position_local is None:
        raise InvenioBibRecordFieldError(
            "A field position is required to "
            "complete this operation.")
    elif field_position_global is not None and \
            field_position_local is not None:
        raise InvenioBibRecordFieldError(
            "Only one field position is required "
            "to complete this operation.")
    elif field_position_global:
        if tag not in rec:
            raise InvenioBibRecordFieldError("No tag '%s' in record." % tag)

        for field in rec[tag]:
            if field[4] == field_position_global:
                return field
        raise InvenioBibRecordFieldError(
            "No field has the tag '%s' and the "
            "global field position '%d'." % (tag, field_position_global))
    else:
        try:
            return rec[tag][field_position_local]
        except KeyError:
            raise InvenioBibRecordFieldError("No tag '%s' in record." % tag)
        except IndexError:
            raise InvenioBibRecordFieldError(
                "No field has the tag '%s' and "
                "the local field position '%d'." % (tag, field_position_local))


def record_replace_field(rec, tag, new_field, field_position_global=None,
                         field_position_local=None):
    """Replace a field with a new field."""
    if field_position_global is None and field_position_local is None:
        raise InvenioBibRecordFieldError(
            "A field position is required to "
            "complete this operation.")
    elif field_position_global is not None and \
            field_position_local is not None:
        raise InvenioBibRecordFieldError(
            "Only one field position is required "
            "to complete this operation.")
    elif field_position_global:
        if tag not in rec:
            raise InvenioBibRecordFieldError("No tag '%s' in record." % tag)

        replaced = False
        for position, field in enumerate(rec[tag]):
            if field[4] == field_position_global:
                rec[tag][position] = new_field
                replaced = True

        if not replaced:
            raise InvenioBibRecordFieldError(
                "No field has the tag '%s' and "
                "the global field position '%d'." %
                (tag, field_position_global))
    else:
        try:
            rec[tag][field_position_local] = new_field
        except KeyError:
            raise InvenioBibRecordFieldError("No tag '%s' in record." % tag)
        except IndexError:
            raise InvenioBibRecordFieldError(
                "No field has the tag '%s' and "
                "the local field position '%d'." % (tag, field_position_local))


def record_get_subfields(rec, tag, field_position_global=None,
                         field_position_local=None):
    """
    Return the subfield of the matching field.

    One has to enter either a global field position or a local field position.

    :return: a list of subfield tuples (subfield code, value).
    :rtype:  list
    """
    field = record_get_field(
        rec, tag,
        field_position_global=field_position_global,
        field_position_local=field_position_local)

    return field[0]


def record_delete_subfield_from(rec, tag, subfield_position,
                                field_position_global=None,
                                field_position_local=None):
    """
    Delete subfield from position specified.

    Specify the subfield by tag, field number and subfield position.
    """
    subfields = record_get_subfields(
        rec, tag,
        field_position_global=field_position_global,
        field_position_local=field_position_local)

    try:
        del subfields[subfield_position]
    except IndexError:
        raise InvenioBibRecordFieldError(
            "The record does not contain the subfield "
            "'%(subfieldIndex)s' inside the field (local: "
            "'%(fieldIndexLocal)s, global: '%(fieldIndexGlobal)s' ) of tag "
            "'%(tag)s'." %
            {"subfieldIndex": subfield_position,
             "fieldIndexLocal": str(field_position_local),
             "fieldIndexGlobal": str(field_position_global),
             "tag": tag})
    if not subfields:
        if field_position_global is not None:
            for position, field in enumerate(rec[tag]):
                if field[4] == field_position_global:
                    del rec[tag][position]
        else:
            del rec[tag][field_position_local]

        if not rec[tag]:
            del rec[tag]


def record_add_subfield_into(rec, tag, subfield_code, value,
                             subfield_position=None,
                             field_position_global=None,
                             field_position_local=None):
    """Add subfield into specified position.

    Specify the subfield by tag, field number and optionally by subfield
    position.
    """
    subfields = record_get_subfields(
        rec, tag,
        field_position_global=field_position_global,
        field_position_local=field_position_local)

    if subfield_position is None:
        subfields.append((subfield_code, value))
    else:
        subfields.insert(subfield_position, (subfield_code, value))


def record_modify_controlfield(rec, tag, controlfield_value,
                               field_position_global=None,
                               field_position_local=None):
    """Modify controlfield at position specified by tag and field number."""
    field = record_get_field(
        rec, tag,
        field_position_global=field_position_global,
        field_position_local=field_position_local)

    new_field = (field[0], field[1], field[2], controlfield_value, field[4])

    record_replace_field(
        rec, tag, new_field,
        field_position_global=field_position_global,
        field_position_local=field_position_local)


def record_modify_subfield(rec, tag, subfield_code, value, subfield_position,
                           field_position_global=None,
                           field_position_local=None):
    """Modify subfield at specified position.

    Specify the subfield by tag, field number and subfield position.
    """
    subfields = record_get_subfields(
        rec, tag,
        field_position_global=field_position_global,
        field_position_local=field_position_local)

    try:
        subfields[subfield_position] = (subfield_code, value)
    except IndexError:
        raise InvenioBibRecordFieldError(
            "There is no subfield with position '%d'." % subfield_position)


def record_move_subfield(rec, tag, subfield_position, new_subfield_position,
                         field_position_global=None,
                         field_position_local=None):
    """Move subfield at specified position.

    Sspecify the subfield by tag, field number and subfield position to new
    subfield position.
    """
    subfields = record_get_subfields(
        rec,
        tag,
        field_position_global=field_position_global,
        field_position_local=field_position_local)

    try:
        subfield = subfields.pop(subfield_position)
        subfields.insert(new_subfield_position, subfield)
    except IndexError:
        raise InvenioBibRecordFieldError(
            "There is no subfield with position '%d'." % subfield_position)


def record_get_field_value(rec, tag, ind1=" ", ind2=" ", code=""):
    """Return first (string) value that matches specified field of the record.

    Returns empty string if not found.

    Parameters (tag, ind1, ind2, code) can contain wildcard %.

    Difference between wildcard % and empty '':

    - Empty char specifies that we are not interested in a field which
      has one of the indicator(s)/subfield specified.

    - Wildcard specifies that we are interested in getting the value
      of the field whatever the indicator(s)/subfield is.

    For e.g. consider the following record in MARC::

        100C5  $$a val1
        555AB  $$a val2
        555AB      val3
        555    $$a val4
        555A       val5

    .. doctest::

        >>> record_get_field_value(record, '555', 'A', '', '')
        "val5"
        >>> record_get_field_value(record, '555', 'A', '%', '')
        "val3"
        >>> record_get_field_value(record, '555', 'A', '%', '%')
        "val2"
        >>> record_get_field_value(record, '555', 'A', 'B', '')
        "val3"
        >>> record_get_field_value(record, '555', '', 'B', 'a')
        ""
        >>> record_get_field_value(record, '555', '', '', 'a')
        "val4"
        >>> record_get_field_value(record, '555', '', '', '')
        ""
        >>> record_get_field_value(record, '%%%', '%', '%', '%')
        "val1"

    :param rec: a record structure as returned by create_record()
    :param tag: a 3 characters long string
    :param ind1: a 1 character long string
    :param ind2: a 1 character long string
    :param code: a 1 character long string
    :return: string value (empty if nothing found)
    """
    # Note: the code is quite redundant for speed reasons (avoid calling
    # functions or doing tests inside loops)
    ind1, ind2 = _wash_indicators(ind1, ind2)

    if '%' in tag:
        # Wild card in tag. Must find all corresponding fields
        if code == '':
            # Code not specified.
            for field_tag, fields in rec.items():
                if _tag_matches_pattern(field_tag, tag):
                    for field in fields:
                        if ind1 in ('%', field[1]) and ind2 in ('%', field[2]):
                            # Return matching field value if not empty
                            if field[3]:
                                return field[3]
        elif code == '%':
            # Code is wildcard. Take first subfield of first matching field
            for field_tag, fields in rec.items():
                if _tag_matches_pattern(field_tag, tag):
                    for field in fields:
                        if (ind1 in ('%', field[1]) and
                                ind2 in ('%', field[2]) and field[0]):
                            return field[0][0][1]
        else:
            # Code is specified. Take corresponding one
            for field_tag, fields in rec.items():
                if _tag_matches_pattern(field_tag, tag):
                    for field in fields:
                        if ind1 in ('%', field[1]) and ind2 in ('%', field[2]):
                            for subfield in field[0]:
                                if subfield[0] == code:
                                    return subfield[1]

    else:
        # Tag is completely specified. Use tag as dict key
        if tag in rec:
            if code == '':
                # Code not specified.
                for field in rec[tag]:
                    if ind1 in ('%', field[1]) and ind2 in ('%', field[2]):
                        # Return matching field value if not empty
                        # or return "" empty if not exist.
                        if field[3]:
                            return field[3]

            elif code == '%':
                # Code is wildcard. Take first subfield of first matching field
                for field in rec[tag]:
                    if ind1 in ('%', field[1]) and ind2 in ('%', field[2]) and\
                            field[0]:
                        return field[0][0][1]
            else:
                # Code is specified. Take corresponding one
                for field in rec[tag]:
                    if ind1 in ('%', field[1]) and ind2 in ('%', field[2]):
                        for subfield in field[0]:
                            if subfield[0] == code:
                                return subfield[1]
    # Nothing was found
    return ""


def record_get_field_values(rec, tag, ind1=" ", ind2=" ", code="",
                            filter_subfield_code="",
                            filter_subfield_value="",
                            filter_subfield_mode="e"):
    """Return the list of values for the specified field of the record.

    List can be filtered. Use filter_subfield_code
    and filter_subfield_value to search
    only in fields that have these values inside them as a subfield.
    filter_subfield_mode can have 3 different values:
    'e' for exact search
    's' for substring search
    'r' for regexp search

    Returns empty list if nothing was found.

    Parameters (tag, ind1, ind2, code) can contain wildcard %.

    :param rec: a record structure as returned by create_record()
    :param tag: a 3 characters long string
    :param ind1: a 1 character long string
    :param ind2: a 1 character long string
    :param code: a 1 character long string
    :return: a list of strings
    """
    tmp = []

    ind1, ind2 = _wash_indicators(ind1, ind2)

    if filter_subfield_code and filter_subfield_mode == "r":
        reg_exp = re.compile(filter_subfield_value)

    tags = []
    if '%' in tag:
        # Wild card in tag. Must find all corresponding tags and fields
        tags = [k for k in rec if _tag_matches_pattern(k, tag)]
    elif rec and tag in rec:
        tags = [tag]

    if code == '':
        # Code not specified. Consider field value (without subfields)
        for tag in tags:
            for field in rec[tag]:
                if (ind1 in ('%', field[1]) and ind2 in ('%', field[2]) and
                        field[3]):
                    tmp.append(field[3])
    elif code == '%':
        # Code is wildcard. Consider all subfields
        for tag in tags:
            for field in rec[tag]:
                if ind1 in ('%', field[1]) and ind2 in ('%', field[2]):
                    if filter_subfield_code:
                        if filter_subfield_mode == "e":
                            subfield_to_match = (filter_subfield_code,
                                                 filter_subfield_value)
                            if subfield_to_match in field[0]:
                                for subfield in field[0]:
                                    tmp.append(subfield[1])
                        elif filter_subfield_mode == "s":
                            if (dict(field[0]).get(filter_subfield_code, '')) \
                                    .find(filter_subfield_value) > -1:
                                for subfield in field[0]:
                                    tmp.append(subfield[1])
                        elif filter_subfield_mode == "r":
                            if reg_exp.match(dict(field[0])
                                             .get(filter_subfield_code, '')):
                                for subfield in field[0]:
                                    tmp.append(subfield[1])
                    else:
                        for subfield in field[0]:
                            tmp.append(subfield[1])
    else:
        # Code is specified. Consider all corresponding subfields
        for tag in tags:
            for field in rec[tag]:
                if ind1 in ('%', field[1]) and ind2 in ('%', field[2]):
                    if filter_subfield_code:
                        if filter_subfield_mode == "e":
                            subfield_to_match = (filter_subfield_code,
                                                 filter_subfield_value)
                            if subfield_to_match in field[0]:
                                for subfield in field[0]:
                                    if subfield[0] == code:
                                        tmp.append(subfield[1])
                        elif filter_subfield_mode == "s":
                            if (dict(field[0]).get(filter_subfield_code, '')) \
                                    .find(filter_subfield_value) > -1:
                                for subfield in field[0]:
                                    if subfield[0] == code:
                                        tmp.append(subfield[1])
                        elif filter_subfield_mode == "r":
                            if reg_exp.match(dict(field[0])
                                             .get(filter_subfield_code, '')):
                                for subfield in field[0]:
                                    if subfield[0] == code:
                                        tmp.append(subfield[1])
                    else:
                        for subfield in field[0]:
                            if subfield[0] == code:
                                tmp.append(subfield[1])

    # If tmp was not set, nothing was found
    return tmp


def record_xml_output(rec, tags=None, order_fn=None):
    """Generate the XML for record 'rec'.

    :param rec: record
    :param tags: list of tags to be printed
    :return: string
    """
    if tags is None:
        tags = []
    if isinstance(tags, str):
        tags = [tags]
    if tags and '001' not in tags:
        # Add the missing controlfield.
        tags.append('001')

    marcxml = ['<record>']

    # Add the tag 'tag' to each field in rec[tag]
    fields = []
    if rec is not None:
        for tag in rec:
            if not tags or tag in tags:
                for field in rec[tag]:
                    fields.append((tag, field))
        if order_fn is None:
            record_order_fields(fields)
        else:
            record_order_fields(fields, order_fn)
        for field in fields:
            marcxml.append(field_xml_output(field[1], field[0]))
    marcxml.append('</record>')
    return '\n'.join(marcxml)


def field_get_subfield_instances(field):
    """Return the list of subfields associated with field 'field'."""
    return field[0]


def field_get_subfield_values(field_instance, code):
    """Return subfield CODE values of the field instance FIELD."""
    return [subfield_value
            for subfield_code, subfield_value in field_instance[0]
            if subfield_code == code]


def field_get_subfield_codes(field_instance):
    """Return subfield codes of the field instance FIELD."""
    return [subfield_code
            for subfield_code, subfield_value in field_instance[0]]


def field_add_subfield(field, code, value):
    """Add a subfield to field 'field'."""
    field[0].append((code, value))


def record_order_fields(rec, fun="_order_by_ord"):
    """Order field inside record 'rec' according to a function."""
    rec.sort(eval(fun))


def field_xml_output(field, tag):
    """Generate the XML for field 'field' and returns it as a string."""
    marcxml = []
    if field[3]:
        marcxml.append('  <controlfield tag="%s">%s</controlfield>' %
                       (tag, escape_for_xml(field[3])))
    else:
        marcxml.append('  <datafield tag="%s" ind1="%s" ind2="%s">' %
                       (tag, field[1], field[2]))
        marcxml += [_subfield_xml_output(subfield) for subfield in field[0]]
        marcxml.append('  </datafield>')
    return '\n'.join(marcxml)


def record_extract_dois(record):
    """Return the DOI(s) of the record."""
    record_dois = []
    tag = "024"
    ind1 = "7"
    ind2 = "_"
    subfield_source_code = "2"
    subfield_value_code = "a"
    identifiers_fields = record_get_field_instances(record, tag, ind1, ind2)
    for identifer_field in identifiers_fields:
        if 'doi' in [val.lower() for val in
                     field_get_subfield_values(identifer_field,
                                               subfield_source_code)]:
            record_dois.extend(
                field_get_subfield_values(
                    identifer_field,
                    subfield_value_code))
    return record_dois


def print_rec(rec, format=1, tags=None):
    """
    Print a record.

    :param format: 1 XML, 2 HTML (not implemented)
    :param tags: list of tags to be printed
    """
    if tags is None:
        tags = []
    if format == 1:
        text = record_xml_output(rec, tags)
    else:
        return ''

    return text


def print_recs(listofrec, format=1, tags=None):
    """
    Print a list of records.

    :param format: 1 XML, 2 HTML (not implemented)
    :param tags: list of tags to be printed
           if 'listofrec' is not a list it returns empty string
    """
    if tags is None:
        tags = []
    text = ""

    if type(listofrec).__name__ != 'list':
        return ""
    else:
        for rec in listofrec:
            text = "%s\n%s" % (text, print_rec(rec, format, tags))
    return text


def concat(alist):
    """Concatenate a list of lists."""
    newl = []
    for l in alist:
        newl.extend(l)
    return newl


def record_find_field(rec, tag, field, strict=False):
    """
    Return the global and local positions of the first occurrence of the field.

    :param rec:    A record dictionary structure
    :type  rec:    dictionary
    :param tag:    The tag of the field to search for
    :type  tag:    string
    :param field:  A field tuple as returned by create_field()
    :type  field:  tuple
    :param strict: A boolean describing the search method. If strict
                   is False, then the order of the subfields doesn't
                   matter. Default search method is strict.
    :type  strict: boolean
    :return:       A tuple of (global_position, local_position) or a
                   tuple (None, None) if the field is not present.
    :rtype:        tuple
    :raise InvenioBibRecordFieldError: If the provided field is invalid.
    """
    try:
        _check_field_validity(field)
    except InvenioBibRecordFieldError:
        raise

    for local_position, field1 in enumerate(rec.get(tag, [])):
        if _compare_fields(field, field1, strict):
            return (field1[4], local_position)

    return (None, None)


def record_match_subfields(rec, tag, ind1=" ", ind2=" ", sub_key=None,
                           sub_value='', sub_key2=None, sub_value2='',
                           case_sensitive=True):
    """
    Find subfield instances in a particular field.

    It tests values in 1 of 3 possible ways:
     - Does a subfield code exist? (ie does 773__a exist?)
     - Does a subfield have a particular value? (ie 773__a == 'PhysX')
     - Do a pair of subfields have particular values?
        (ie 035__2 == 'CDS' and 035__a == '123456')

    Parameters:
     * rec - dictionary: a bibrecord structure
     * tag - string: the tag of the field (ie '773')
     * ind1, ind2 - char: a single characters for the MARC indicators
     * sub_key - char: subfield key to find
     * sub_value - string: subfield value of that key
     * sub_key2 - char: key of subfield to compare against
     * sub_value2 - string: expected value of second subfield
     * case_sensitive - bool: be case sensitive when matching values

    :return: false if no match found, else provides the field position (int)
    """
    if sub_key is None:
        raise TypeError("None object passed for parameter sub_key.")

    if sub_key2 is not None and sub_value2 is '':
        raise TypeError("Parameter sub_key2 defined but sub_value2 is None, "
                        + "function requires a value for comparrison.")
    ind1, ind2 = _wash_indicators(ind1, ind2)

    if not case_sensitive:
        sub_value = sub_value.lower()
        sub_value2 = sub_value2.lower()

    for field in record_get_field_instances(rec, tag, ind1, ind2):
        subfields = dict(field_get_subfield_instances(field))
        if not case_sensitive:
            for k, v in subfields.iteritems():
                subfields[k] = v.lower()

        if sub_key in subfields:
            if sub_value is '':
                return field[4]
            else:
                if sub_value == subfields[sub_key]:
                    if sub_key2 is None:
                        return field[4]
                    else:
                        if sub_key2 in subfields:
                            if sub_value2 == subfields[sub_key2]:
                                return field[4]
    return False


def record_strip_empty_volatile_subfields(rec):
    """Remove unchanged volatile subfields from the record."""
    for tag in rec.keys():
        for field in rec[tag]:
            field[0][:] = [subfield for subfield in field[0]
                           if subfield[1][:9] != "VOLATILE:"]


def record_make_all_subfields_volatile(rec):
    """
    Turns all subfields to volatile
    """
    for tag in rec.keys():
        for field_position, field in enumerate(rec[tag]):
            for subfield_position, subfield in enumerate(field[0]):
                if subfield[1][:9] != "VOLATILE:":
                    record_modify_subfield(rec, tag, subfield[0], "VOLATILE:" + subfield[1],
                        subfield_position, field_position_local=field_position)

def record_strip_empty_fields(rec, tag=None):
    """
    Remove empty subfields and fields from the record.

    If 'tag' is not None, only a specific tag of the record will be stripped,
    otherwise the whole record.

    :param rec:  A record dictionary structure
    :type  rec:  dictionary
    :param tag:  The tag of the field to strip empty fields from
    :type  tag:  string
    """
    # Check whole record
    if tag is None:
        tags = rec.keys()
        for tag in tags:
            record_strip_empty_fields(rec, tag)

    # Check specific tag of the record
    elif tag in rec:
        # in case of a controlfield
        if tag[:2] == '00':
            if len(rec[tag]) == 0 or not rec[tag][0][3]:
                del rec[tag]

        #in case of a normal field
        else:
            fields = []
            for field in rec[tag]:
                subfields = []
                for subfield in field[0]:
                    # check if the subfield has been given a value
                    if subfield[1]:
                        # Always strip values
                        subfield = (subfield[0], subfield[1].strip())
                        subfields.append(subfield)
                if len(subfields) > 0:
                    new_field = create_field(subfields, field[1], field[2],
                                             field[3])
                    fields.append(new_field)
            if len(fields) > 0:
                rec[tag] = fields
            else:
                del rec[tag]


def record_strip_controlfields(rec):
    """
    Remove all non-empty controlfields from the record.

    :param rec:  A record dictionary structure
    :type  rec:  dictionary
    """
    for tag in rec.keys():
        if tag[:2] == '00' and rec[tag][0][3]:
            del rec[tag]


def record_order_subfields(rec, tag=None):
    """
    Order subfields from a record alphabetically based on subfield code.

    If 'tag' is not None, only a specific tag of the record will be reordered,
    otherwise the whole record.

    :param rec: bibrecord
    :type rec: bibrec
    :param tag: tag where the subfields will be ordered
    :type tag: str
    """
    if rec is None:
        return rec
    if tag is None:
        tags = rec.keys()
        for tag in tags:
            record_order_subfields(rec, tag)
    elif tag in rec:
        for i in xrange(len(rec[tag])):
            field = rec[tag][i]
            # Order subfields alphabetically by subfield code
            ordered_subfields = sorted(field[0],
                                       key=lambda subfield: subfield[0])
            rec[tag][i] = (ordered_subfields, field[1], field[2], field[3],
                           field[4])


def record_empty(rec):
    """TODO."""
    for key in rec.iterkeys():
        if key not in ('001', '005'):
            return False
    return True


def field_get_subfields(field):
    """ Given a field, will place all subfields into a dictionary
    Parameters:
     * field - tuple: The field to get subfields for
    Returns: a dictionary, codes as keys and a list of values as the value """
    pairs = {}
    for key, value in field[0]:
        if key in pairs and pairs[key] != value:
            pairs[key].append(value)
        else:
            pairs[key] = [value]
    return pairs


def field_swap_subfields(field, subs):
    """ Recreates a field with a new set of subfields """
    return subs, field[1], field[2], field[3], field[4]


def _compare_fields(field1, field2, strict=True):
    """
    Compare 2 fields.

    If strict is True, then the order of the subfield will be taken care of, if
    not then the order of the subfields doesn't matter.

    :return: True if the field are equivalent, False otherwise.
    """
    if strict:
        # Return a simple equal test on the field minus the position.
        return field1[:4] == field2[:4]
    else:
        if field1[1:4] != field2[1:4]:
            # Different indicators or controlfield value.
            return False
        else:
            # Compare subfields in a loose way.
            return set(field1[0]) == set(field2[0])


def _check_field_validity(field):
    """
    Check if a field is well-formed.

    :param field: A field tuple as returned by create_field()
    :type field:  tuple
    :raise InvenioBibRecordFieldError: If the field is invalid.
    """
    if type(field) not in (list, tuple):
        raise InvenioBibRecordFieldError(
            "Field of type '%s' should be either "
            "a list or a tuple." % type(field))

    if len(field) != 5:
        raise InvenioBibRecordFieldError(
            "Field of length '%d' should have 5 "
            "elements." % len(field))

    if type(field[0]) not in (list, tuple):
        raise InvenioBibRecordFieldError(
            "Subfields of type '%s' should be "
            "either a list or a tuple." % type(field[0]))

    if type(field[1]) is not str:
        raise InvenioBibRecordFieldError(
            "Indicator 1 of type '%s' should be "
            "a string." % type(field[1]))

    if type(field[2]) is not str:
        raise InvenioBibRecordFieldError(
            "Indicator 2 of type '%s' should be "
            "a string." % type(field[2]))

    if type(field[3]) is not str:
        raise InvenioBibRecordFieldError(
            "Controlfield value of type '%s' "
            "should be a string." % type(field[3]))

    if type(field[4]) is not int:
        raise InvenioBibRecordFieldError(
            "Global position of type '%s' should "
            "be an int." % type(field[4]))

    for subfield in field[0]:
        if (type(subfield) not in (list, tuple) or
                len(subfield) != 2 or type(subfield[0]) is not str or
                type(subfield[1]) is not str):
            raise InvenioBibRecordFieldError(
                "Subfields are malformed. "
                "Should a list of tuples of 2 strings.")


def _shift_field_positions_global(record, start, delta=1):
    """
    Shift all global field positions.

    Shift all global field positions with global field positions
    higher or equal to 'start' from the value 'delta'.
    """
    if not delta:
        return

    for tag, fields in record.items():
        newfields = []
        for field in fields:
            if field[4] < start:
                newfields.append(field)
            else:
                # Increment the global field position by delta.
                newfields.append(tuple(list(field[:4]) + [field[4] + delta]))
        record[tag] = newfields


def _tag_matches_pattern(tag, pattern):
    """Return true if MARC 'tag' matches a 'pattern'.

    'pattern' is plain text, with % as wildcard

    Both parameters must be 3 characters long strings.

    .. doctest::

        >>> _tag_matches_pattern("909", "909")
        True
        >>> _tag_matches_pattern("909", "9%9")
        True
        >>> _tag_matches_pattern("909", "9%8")
        False

    :param tag: a 3 characters long string
    :param pattern: a 3 characters long string
    :return: False or True
    """
    for char1, char2 in zip(tag, pattern):
        if char2 not in ('%', char1):
            return False
    return True


def _validate_record_field_positions_global(record):
    """
    Check if the global field positions in the record are valid.

    I.e., no duplicate global field positions and local field positions in the
    list of fields are ascending.

    :param record: the record data structure
    :return: the first error found as a string or None if no error was found
    """
    all_fields = []
    for tag, fields in record.items():
        previous_field_position_global = -1
        for field in fields:
            if field[4] < previous_field_position_global:
                return ("Non ascending global field positions in tag '%s'." %
                        tag)
            previous_field_position_global = field[4]
            if field[4] in all_fields:
                return ("Duplicate global field position '%d' in tag '%s'" %
                        (field[4], tag))


def _record_sort_by_indicators(record):
    """Sort the fields inside the record by indicators."""
    for tag, fields in record.items():
        record[tag] = _fields_sort_by_indicators(fields)


def _fields_sort_by_indicators(fields):
    """Sort a set of fields by their indicators.

    Return a sorted list with correct global field positions.
    """
    field_dict = {}
    field_positions_global = []
    for field in fields:
        field_dict.setdefault(field[1:3], []).append(field)
        field_positions_global.append(field[4])

    indicators = field_dict.keys()
    indicators.sort()

    field_list = []
    for indicator in indicators:
        for field in field_dict[indicator]:
            field_list.append(field[:4] + (field_positions_global.pop(0),))

    return field_list


def _create_record_lxml(marcxml,
                        verbose=CFG_BIBRECORD_DEFAULT_VERBOSE_LEVEL,
                        correct=CFG_BIBRECORD_DEFAULT_CORRECT,
                        keep_singletons=CFG_BIBRECORD_KEEP_SINGLETONS):
    """
    Create a record object using the LXML parser.

    If correct == 1, then perform DTD validation
    If correct == 0, then do not perform DTD validation

    If verbose == 0, the parser will not give warnings.
    If 1 <= verbose <= 3, the parser will not give errors, but will warn
        the user about possible mistakes (implement me!)
    If verbose > 3 then the parser will be strict and will stop in case of
        well-formedness errors or DTD errors.

    """
    parser = etree.XMLParser(dtd_validation=correct,
                             recover=(verbose <= 3))
    if correct:
        marcxml = '<?xml version="1.0" encoding="UTF-8"?>\n' \
                  '<collection>\n%s\n</collection>' % (marcxml,)
    try:
        tree = etree.parse(StringIO(marcxml), parser)
        # parser errors are located in parser.error_log
        # if 1 <= verbose <=3 then show them to the user?
        # if verbose == 0 then continue
        # if verbose >3 then an exception will be thrown
    except Exception as e:
        raise InvenioBibRecordParserError(str(e))

    record = {}
    field_position_global = 0

    controlfield_iterator = tree.iter(tag='{*}controlfield')
    for controlfield in controlfield_iterator:
        tag = controlfield.attrib.get('tag', '!').encode("UTF-8")
        ind1 = ' '
        ind2 = ' '
        text = controlfield.text
        if text is None:
            text = ''
        else:
            text = text.encode("UTF-8")
        subfields = []
        if text or keep_singletons:
            field_position_global += 1
            record.setdefault(tag, []).append((subfields, ind1, ind2, text,
                                               field_position_global))

    datafield_iterator = tree.iter(tag='{*}datafield')
    for datafield in datafield_iterator:
        tag = datafield.attrib.get('tag', '!').encode("UTF-8")
        ind1 = datafield.attrib.get('ind1', '!').encode("UTF-8")
        ind2 = datafield.attrib.get('ind2', '!').encode("UTF-8")
        if ind1 in ('', '_'):
            ind1 = ' '
        if ind2 in ('', '_'):
            ind2 = ' '
        subfields = []
        subfield_iterator = datafield.iter(tag='{*}subfield')
        for subfield in subfield_iterator:
            code = subfield.attrib.get('code', '!').encode("UTF-8")
            text = subfield.text
            if text is None:
                text = ''
            else:
                text = text.encode("UTF-8")
            if text or keep_singletons:
                subfields.append((code, text))
        if subfields or keep_singletons:
            text = ''
            field_position_global += 1
            record.setdefault(tag, []).append((subfields, ind1, ind2, text,
                                               field_position_global))

    return record


def _concat(alist):
    """Concatenate a list of lists."""
    return [element for single_list in alist for element in single_list]


def _subfield_xml_output(subfield):
    """Generate the XML for a subfield object and return it as a string."""
    return '    <subfield code="%s">%s</subfield>' % \
        (subfield[0], escape_for_xml(subfield[1]))


def _order_by_ord(field1, field2):
    """Function used to order the fields according to their ord value."""
    return cmp(field1[1][4], field2[1][4])


def _order_by_tags(field1, field2):
    """Function used to order the fields according to the tags."""
    return cmp(field1[0], field2[0])


def _get_children_by_tag_name(node, name):
    """Retrieve all children from node 'node' with name 'name'."""
    try:
        return [child for child in node.childNodes if child.nodeName == name]
    except TypeError:
        return []


def _get_children_as_string(node):
    """Iterate through all the children of a node.

    Returns one string containing the values from all the text-nodes
    recursively.
    """
    out = []
    if node:
        for child in node:
            if child.nodeType == child.TEXT_NODE:
                out.append(child.data)
            else:
                out.append(_get_children_as_string(child.childNodes))
    return ''.join(out)


def _wash_indicators(*indicators):
    """
    Wash the values of the indicators.

    An empty string or an underscore is replaced by a blank space.

    :param indicators: a series of indicators to be washed
    :return: a list of washed indicators
    """
    return [indicator in ('', '_') and ' ' or indicator
            for indicator in indicators]


def _correct_record(record):
    """
    Check and correct the structure of the record.

    :param record: the record data structure
    :return: a list of errors found
    """
    errors = []

    for tag in record.keys():
        upper_bound = '999'
        n = len(tag)

        if n > 3:
            i = n - 3
            while i > 0:
                upper_bound = '%s%s' % ('0', upper_bound)
                i -= 1

        # Missing tag. Replace it with dummy tag '000'.
        if tag == '!':
            errors.append((1, '(field number(s): ' +
                              str([f[4] for f in record[tag]]) + ')'))
            record['000'] = record.pop(tag)
            tag = '000'
        elif not ('001' <= tag <= upper_bound or
                  tag in ('FMT', 'FFT', 'BDR', 'BDM')):
            errors.append(2)
            record['000'] = record.pop(tag)
            tag = '000'

        fields = []
        for field in record[tag]:
            # Datafield without any subfield.
            if field[0] == [] and field[3] == '':
                errors.append((8, '(field number: ' + str(field[4]) + ')'))

            subfields = []
            for subfield in field[0]:
                if subfield[0] == '!':
                    errors.append((3, '(field number: ' + str(field[4]) + ')'))
                    newsub = ('', subfield[1])
                else:
                    newsub = subfield
                subfields.append(newsub)

            if field[1] == '!':
                errors.append((4, '(field number: ' + str(field[4]) + ')'))
                ind1 = " "
            else:
                ind1 = field[1]

            if field[2] == '!':
                errors.append((5, '(field number: ' + str(field[4]) + ')'))
                ind2 = " "
            else:
                ind2 = field[2]

            fields.append((subfields, ind1, ind2, field[3], field[4]))

        record[tag] = fields

    return errors


def _warning(code):
    """
    Return a warning message of code 'code'.

    If code = (cd, str) it returns the warning message of code 'cd' and appends
    str at the end
    """
    if isinstance(code, str):
        return code

    message = ''
    if isinstance(code, tuple):
        if isinstance(code[0], str):
            message = code[1]
            code = code[0]
    return CFG_BIBRECORD_WARNING_MSGS.get(code, '') + message


def _warnings(alist):
    """Apply the function _warning() to every element in alist."""
    return [_warning(element) for element in alist]


def _compare_lists(list1, list2, custom_cmp):
    """Compare twolists using given comparing function.

    :param list1: first list to compare
    :param list2: second list to compare
    :param custom_cmp: a function taking two arguments (element of
        list 1, element of list 2) and
    :return: True or False depending if the values are the same
    """
    if len(list1) != len(list2):
        return False
    for element1, element2 in zip(list1, list2):
        if not custom_cmp(element1, element2):
            return False
    return True
