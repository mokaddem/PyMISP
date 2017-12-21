#!/usr/bin/env python
# -*- coding: utf-8 -*-

import abc
import sys
import datetime
import json
from json import JSONEncoder
import collections
import six  # Remove that import when discarding python2 support.
import logging

logger = logging.getLogger('pymisp')

if six.PY2:
    logger.warning("You're using python 2, it is strongly recommended to use python >=3.5")


class MISPEncode(JSONEncoder):

    def default(self, obj):
        if isinstance(obj, AbstractMISP):
            return obj.jsonable()
        return JSONEncoder.default(self, obj)


@six.add_metaclass(abc.ABCMeta)   # Remove that line when discarding python2 support.
class AbstractMISP(collections.MutableMapping):

    __not_jsonable = []

    def __init__(self, **kwargs):
        super(AbstractMISP, self).__init__()
        self.__edited = True

    @property
    def properties(self):
        to_return = []
        for prop, value in vars(self).items():
            if prop.startswith('_') or prop in self.__not_jsonable:
                continue
            to_return.append(prop)
        return to_return

    def from_dict(self, **kwargs):
        for prop, value in kwargs.items():
            if value is None:
                continue
            setattr(self, prop, value)
        # We load an existing dictionary, marking it an not-edited
        self.__edited = False

    def update_not_jsonable(self, *args):
        self.__not_jsonable += args

    def set_not_jsonable(self, *args):
        self.__not_jsonable = args

    def from_json(self, json_string):
        """Load a JSON string"""
        self.from_dict(json.loads(json_string))

    def to_dict(self):
        to_return = {}
        for attribute in self.properties:
            val = getattr(self, attribute, None)
            if val is None:
                continue
            if attribute == 'timestamp':
                if self.edited:
                    # In order to be accepted by MISP, the timestamp of an object
                    # needs to be either newer, or None.
                    # If the current object is marked as edited, the easiest is to
                    # skip the timestamp and let MISP deal with it
                    continue
                else:
                    val = self._datetime_to_timestamp(val)
            to_return[attribute] = val
        return to_return

    def jsonable(self):
        return self.to_dict()

    def to_json(self):
        return json.dumps(self, cls=MISPEncode)

    def __getitem__(self, key):
        try:
            return getattr(self, key)
        except AttributeError:
            # Expected by pop and other dict-related methods
            raise KeyError

    def __setitem__(self, key, value):
        setattr(self, key, value)

    def __delitem__(self, key):
        delattr(self, key)

    def __iter__(self):
        return iter(self.to_dict())

    def __len__(self):
        return len(self.to_dict())

    @property
    def edited(self):
        if self.__edited:
            return self.__edited
        for p in self.properties:
            if self.__edited:
                break
            if isinstance(p, AbstractMISP) and p.edited:
                self.__edited = True
            elif isinstance(p, list) and all(isinstance(a, AbstractMISP) for a in p):
                if any(a.edited for a in p):
                    self.__edited = True
        return self.__edited

    @edited.setter
    def edited(self, val):
        if isinstance(val, bool):
            self.__edited = val
        else:
            raise Exception('edited can only be True or False')

    def __setattr__(self, name, value):
        if name in self.properties:
            self.__edited = True
        super(AbstractMISP, self).__setattr__(name, value)

    def _datetime_to_timestamp(self, d):
        if isinstance(d, (int, str)):
            # Assume we already have a timestamp
            return d
        if sys.version_info >= (3, 3):
            return d.timestamp()
        else:
            return (d - datetime.datetime.utcfromtimestamp(0)).total_seconds()
