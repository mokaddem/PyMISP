#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from typing import TypeVar, Optional, Tuple, List, Dict, Union, Any
from datetime import date, datetime
import csv
from pathlib import Path
import logging
from urllib.parse import urljoin
import json
import requests
from requests.auth import AuthBase
import re
from uuid import UUID
import warnings
import sys
import traceback

from . import __version__, everything_broken
from .exceptions import MISPServerError, PyMISPUnexpectedResponse, PyMISPNotImplementedYet, PyMISPError, NoURL, NoKey
from .mispevent import MISPEvent, MISPAttribute, MISPSighting, MISPLog, MISPObject, \
    MISPUser, MISPOrganisation, MISPShadowAttribute, MISPWarninglist, MISPTaxonomy, \
    MISPGalaxy, MISPNoticelist, MISPObjectReference, MISPObjectTemplate, MISPSharingGroup, \
    MISPRole, MISPServer, MISPFeed, MISPEventDelegation, MISPCommunity, MISPUserSetting
from .abstract import pymisp_json_default, MISPTag, AbstractMISP, describe_types

SearchType = TypeVar('SearchType', str, int)
# str: string to search / list: values to search (OR) / dict: {'OR': [list], 'NOT': [list], 'AND': [list]}
SearchParameterTypes = TypeVar('SearchParameterTypes', str, List[Union[str, int]], Dict[str, Union[str, int]])

ToIDSType = TypeVar('ToIDSType', str, int, bool)

logger = logging.getLogger('pymisp')


class PyMISP:
    """Python API for MISP

    :param url: URL of the MISP instance you want to connect to
    :param key: API key of the user you want to use
    :param ssl: can be True or False (to check ot not the validity of the certificate. Or a CA_BUNDLE in case of self signed certificate (the concatenation of all the *.crt of the chain)
    :param debug: Write all the debug information to stderr
    :param proxies: Proxy dict as describes here: http://docs.python-requests.org/en/master/user/advanced/#proxies
    :param cert: Client certificate, as described there: http://docs.python-requests.org/en/master/user/advanced/#client-side-certificates
    :param auth: The auth parameter is passed directly to requests, as described here: http://docs.python-requests.org/en/master/user/authentication/
    :param tool: The software using PyMISP (string), used to set a unique user-agent
    """

    def __init__(self, url: str, key: str, ssl=True, debug: bool=False, proxies: dict={},
                 cert: Tuple[str, tuple]=None, auth: AuthBase=None, tool: str=''):
        if not url:
            raise NoURL('Please provide the URL of your MISP instance.')
        if not key:
            raise NoKey('Please provide your authorization key.')

        self.root_url = url
        self.key = key
        self.ssl = ssl
        self.proxies = proxies
        self.cert = cert
        self.auth = auth
        self.tool = tool

        self.global_pythonify = False

        self.resources_path = Path(__file__).parent / 'data'
        if debug:
            logger.setLevel(logging.DEBUG)
            logger.info('To configure logging in your script, leave it to None and use the following: import logging; logging.getLogger(\'pymisp\').setLevel(logging.DEBUG)')

        try:
            # Make sure the MISP instance is working and the URL is valid
            response = self.recommended_pymisp_version
            if 'errors' in response:
                logger.warning(response['errors'][0])
            else:
                pymisp_version_tup = tuple(int(x) for x in __version__.split('.'))
                recommended_version_tup = tuple(int(x) for x in response['version'].split('.'))
                if recommended_version_tup < pymisp_version_tup[:3]:
                    logger.info(f"The version of PyMISP recommended by the MISP instance (response['version']) is older than the one you're using now ({__version__}). If you have a problem, please upgrade the MISP instance or use an older PyMISP version.")
                elif pymisp_version_tup[:3] < recommended_version_tup:
                    logger.warning(f"The version of PyMISP recommended by the MISP instance ({response['version']}) is newer than the one you're using now ({__version__}). Please upgrade PyMISP.")

            misp_version = self.misp_instance_version
            if 'version' in misp_version:
                self._misp_version = tuple(int(v) for v in misp_version['version'].split('.'))

            # Get the user information
            self._current_user: MISPUser
            self._current_role: MISPRole
            self._current_user_settings: List[MISPUserSetting]
            self._current_user, self._current_role, self._current_user_settings = self.get_user(pythonify=True, expanded=True)
        except Exception as e:
            traceback.print_exc()
            raise PyMISPError(f'Unable to connect to MISP ({self.root_url}). Please make sure the API key and the URL are correct (http/https is required): {e}')

        try:
            self.describe_types = self.describe_types_remote
        except Exception:
            self.describe_types = self.describe_types_local

        self.categories = self.describe_types['categories']
        self.types = self.describe_types['types']
        self.category_type_mapping = self.describe_types['category_type_mappings']
        self.sane_default = self.describe_types['sane_defaults']

    def remote_acl(self, debug_type: str='findMissingFunctionNames') -> dict:
        """This should return an empty list, unless the ACL is outdated.
        debug_type can only be printAllFunctionNames, findMissingFunctionNames, or printRoleAccess
        """
        response = self._prepare_request('GET', f'events/queryACL/{debug_type}')
        return self._check_json_response(response)

    @property
    def describe_types_local(self) -> dict:
        '''Returns the content of describe types from the package'''
        return describe_types

    @property
    def describe_types_remote(self) -> dict:
        '''Returns the content of describe types from the remote instance'''
        response = self._prepare_request('GET', 'attributes/describeTypes.json')
        remote_describe_types = self._check_json_response(response)
        return remote_describe_types['result']

    @property
    def recommended_pymisp_version(self) -> dict:
        """Returns the recommended API version from the server"""
        response = self._prepare_request('GET', 'servers/getPyMISPVersion.json')
        return self._check_json_response(response)

    @property
    def version(self) -> dict:
        """Returns the version of PyMISP you're curently using"""
        return {'version': __version__}

    @property
    def pymisp_version_master(self) -> dict:
        """Get the most recent version of PyMISP from github"""
        r = requests.get('https://raw.githubusercontent.com/MISP/PyMISP/master/pymisp/__init__.py')
        if r.status_code == 200:
            version = re.findall("__version__ = '(.*)'", r.text)
            return {'version': version[0]}
        return {'error': 'Impossible to retrieve the version of the master branch.'}

    @property
    def misp_instance_version(self) -> dict:
        """Returns the version of the instance."""
        response = self._prepare_request('GET', 'servers/getVersion.json')
        return self._check_json_response(response)

    @property
    def misp_instance_version_master(self) -> dict:
        """Get the most recent version from github"""
        r = requests.get('https://raw.githubusercontent.com/MISP/MISP/2.4/VERSION.json')
        if r.status_code == 200:
            master_version = json.loads(r.text)
            return {'version': '{}.{}.{}'.format(master_version['major'], master_version['minor'], master_version['hotfix'])}
        return {'error': 'Impossible to retrieve the version of the master branch.'}

    def update_misp(self) -> dict:
        response = self._prepare_request('POST', '/servers/update')
        return self._check_json_response(response)

    def set_server_setting(self, setting: str, value: Union[str, int, bool], force: bool=False) -> dict:
        data = {'value': value, 'force': force}
        response = self._prepare_request('POST', f'/servers/serverSettingsEdit/{setting}', data=data)
        return self._check_json_response(response)

    def get_server_setting(self, setting: str) -> dict:
        response = self._prepare_request('GET', f'/servers/getSetting/{setting}')
        return self._check_json_response(response)

    def server_settings(self) -> dict:
        response = self._prepare_request('GET', f'/servers/serverSettings')
        return self._check_json_response(response)

    def restart_workers(self) -> dict:
        response = self._prepare_request('POST', f'/servers/restartWorkers')
        return self._check_json_response(response)

    def db_schema_diagnostic(self) -> dict:
        response = self._prepare_request('GET', f'/servers/dbSchemaDiagnostic')
        return self._check_json_response(response)

    def toggle_global_pythonify(self) -> None:
        self.global_pythonify = not self.global_pythonify

    # ## BEGIN Event ##

    def events(self, pythonify: bool=False) -> Union[dict, List[MISPEvent]]:
        r = self._prepare_request('GET', 'events')
        events_r = self._check_json_response(r)
        if not (self.global_pythonify or pythonify) or 'errors' in events_r:
            return events_r
        to_return = []
        for event in events_r:
            e = MISPEvent()
            e.from_dict(**event)
            to_return.append(e)
        return to_return

    def get_event(self, event: Union[MISPEvent, int, str, UUID], deleted: Union[bool, int, list]=False, extended: bool = False, pythonify: bool=False) -> Union[dict, MISPEvent]:
        '''Get an event from a MISP instance'''
        event_id = self.__get_uuid_or_id_from_abstract_misp(event)
        if deleted:
            data = {'deleted': deleted}
            if extended:
                r = self._prepare_request('POST', f'events/view/{event_id}/extended:{event_id}', data=data)
            else:
                r = self._prepare_request('POST', f'events/view/{event_id}', data=data)
        else:
            if extended:
                r = self._prepare_request('GET', f'events/view/{event_id}/extended:{event_id}')
            else:
                r = self._prepare_request('GET', f'events/view/{event_id}')
        event_r = self._check_json_response(r)
        if not (self.global_pythonify or pythonify) or 'errors' in event_r:
            return event_r
        e = MISPEvent()
        e.load(event_r)
        return e

    def add_event(self, event: MISPEvent, pythonify: bool=False) -> Union[dict, MISPEvent]:
        '''Add a new event on a MISP instance'''
        r = self._prepare_request('POST', 'events', data=event)
        new_event = self._check_json_response(r)
        if not (self.global_pythonify or pythonify) or 'errors' in new_event:
            return new_event
        e = MISPEvent()
        e.load(new_event)
        return e

    def update_event(self, event: MISPEvent, event_id: Optional[int]=None, pythonify: bool=False) -> Union[dict, MISPEvent]:
        '''Update an event on a MISP instance'''
        if event_id is None:
            eid = self.__get_uuid_or_id_from_abstract_misp(event)
        else:
            eid = self.__get_uuid_or_id_from_abstract_misp(event_id)
        r = self._prepare_request('POST', f'events/{eid}', data=event)
        updated_event = self._check_json_response(r)
        if not (self.global_pythonify or pythonify) or 'errors' in updated_event:
            return updated_event
        e = MISPEvent()
        e.load(updated_event)
        return e

    def extend_event(self, event: MISPEvent, event_id: int, pythonify: bool=False) -> Union[dict, MISPEvent]:
        '''Extends an event on a MISP instance'''
        eid = self.__get_uuid_or_id_from_abstract_misp(event_id)
        r = self._prepare_request('POST', f'events/add/extends/{eid}', data=event)
        updated_event = self._check_json_response(r)
        if not (self.global_pythonify or pythonify) or 'errors' in updated_event:
            return updated_event
        e = MISPEvent()
        e.load(updated_event)
        return e

    def delete_event(self, event: Union[MISPEvent, int, str, UUID]) -> dict:
        '''Delete an event from a MISP instance'''
        event_id = self.__get_uuid_or_id_from_abstract_misp(event)
        response = self._prepare_request('DELETE', f'events/delete/{event_id}')
        return self._check_json_response(response)

    def publish(self, event: Union[MISPEvent, int, str, UUID], alert: bool=False) -> dict:
        """Publish the event with one single HTTP POST.
        The default is to not send a mail as it is assumed this method is called on update.
        """
        event_id = self.__get_uuid_or_id_from_abstract_misp(event)
        if alert:
            response = self._prepare_request('POST', f'events/alert/{event_id}')
        else:
            response = self._prepare_request('POST', f'events/publish/{event_id}')
        return self._check_json_response(response)

    def contact_event_reporter(self, event: Union[MISPEvent, int, str, UUID], message: str) -> dict:
        """Send a message to the reporter of an event"""
        event_id = self.__get_uuid_or_id_from_abstract_misp(event)
        to_post = {'message': message}
        response = self._prepare_request('POST', f'events/contact/{event_id}', data=to_post)
        return self._check_json_response(response)

    # ## END Event ###

    # ## BEGIN Object ###

    def get_object(self, misp_object: Union[MISPObject, int, str, UUID], pythonify: bool=False) -> Union[dict, MISPObject]:
        '''Get an object from the remote MISP instance'''
        object_id = self.__get_uuid_or_id_from_abstract_misp(misp_object)
        r = self._prepare_request('GET', f'objects/view/{object_id}')
        misp_object_r = self._check_json_response(r)
        if not (self.global_pythonify or pythonify) or 'errors' in misp_object_r:
            return misp_object_r
        o = MISPObject(misp_object_r['Object']['name'])
        o.from_dict(**misp_object_r)
        return o

    def add_object(self, event: Union[MISPEvent, int, str, UUID], misp_object: MISPObject, pythonify: bool=False) -> Union[dict, MISPObject]:
        '''Add a MISP Object to an existing MISP event'''
        event_id = self.__get_uuid_or_id_from_abstract_misp(event)
        r = self._prepare_request('POST', f'objects/add/{event_id}', data=misp_object)
        new_object = self._check_json_response(r)
        if not (self.global_pythonify or pythonify) or 'errors' in new_object:
            return new_object
        o = MISPObject(new_object['Object']['name'])
        o.from_dict(**new_object)
        return o

    def update_object(self, misp_object: MISPObject, object_id: int=None, pythonify: bool=False) -> Union[dict, MISPObject]:
        '''Update an object on a MISP instance'''
        if object_id is None:
            oid = self.__get_uuid_or_id_from_abstract_misp(misp_object)
        else:
            oid = self.__get_uuid_or_id_from_abstract_misp(object_id)
        r = self._prepare_request('POST', f'objects/edit/{oid}', data=misp_object)
        updated_object = self._check_json_response(r)
        if not (self.global_pythonify or pythonify) or 'errors' in updated_object:
            return updated_object
        o = MISPObject(updated_object['Object']['name'])
        o.from_dict(**updated_object)
        return o

    def delete_object(self, misp_object: Union[MISPObject, int, str, UUID]) -> dict:
        '''Delete an object from a MISP instance'''
        object_id = self.__get_uuid_or_id_from_abstract_misp(misp_object)
        response = self._prepare_request('POST', f'objects/delete/{object_id}')
        return self._check_json_response(response)

    def add_object_reference(self, misp_object_reference: MISPObjectReference, pythonify: bool=False) -> Union[dict, MISPObjectReference]:
        """Add a reference to an object"""
        r = self._prepare_request('POST', 'object_references/add', misp_object_reference)
        object_reference = self._check_json_response(r)
        if not (self.global_pythonify or pythonify) or 'errors' in object_reference:
            return object_reference
        ref = MISPObjectReference()
        ref.from_dict(**object_reference)
        return ref

    def delete_object_reference(self, object_reference: Union[MISPObjectReference, int, str, UUID]) -> dict:
        """Delete a reference to an object"""
        object_reference_id = self.__get_uuid_or_id_from_abstract_misp(object_reference)
        response = self._prepare_request('POST', f'object_references/delete/{object_reference_id}')
        return self._check_json_response(response)

    # Object templates

    def object_templates(self, pythonify: bool=False) -> Union[dict, List[MISPObjectTemplate]]:
        """Get all the object templates."""
        r = self._prepare_request('GET', 'objectTemplates')
        templates = self._check_json_response(r)
        if not (self.global_pythonify or pythonify) or 'errors' in templates:
            return templates
        to_return = []
        for object_template in templates:
            o = MISPObjectTemplate()
            o.from_dict(**object_template)
            to_return.append(o)
        return to_return

    def get_object_template(self, object_template: Union[MISPObjectTemplate, int, str, UUID], pythonify: bool=False) -> Union[dict, MISPObjectTemplate]:
        """Gets the full object template corresponting the UUID passed as parameter"""
        object_template_id = self.__get_uuid_or_id_from_abstract_misp(object_template)
        r = self._prepare_request('GET', f'objectTemplates/view/{object_template_id}')
        object_template_r = self._check_json_response(r)
        if not (self.global_pythonify or pythonify) or 'errors' in object_template_r:
            return object_template_r
        t = MISPObjectTemplate()
        t.from_dict(**object_template_r)
        return t

    def update_object_templates(self) -> dict:
        """Trigger an update of the object templates"""
        response = self._prepare_request('POST', 'objectTemplates/update')
        return self._check_json_response(response)

    # ## END Object ###

    # ## BEGIN Attribute ###

    def attributes(self, pythonify: bool=False) -> Union[dict, List[MISPAttribute]]:
        r = self._prepare_request('GET', f'attributes/index')
        attributes_r = self._check_json_response(r)
        if not (self.global_pythonify or pythonify) or 'errors' in attributes_r:
            return attributes_r
        to_return = []
        for attribute in attributes_r:
            a = MISPAttribute()
            a.from_dict(**attribute)
            to_return.append(a)
        return to_return

    def get_attribute(self, attribute: Union[MISPAttribute, int, str, UUID], pythonify: bool=False) -> Union[dict, MISPAttribute]:
        '''Get an attribute from a MISP instance'''
        attribute_id = self.__get_uuid_or_id_from_abstract_misp(attribute)
        r = self._prepare_request('GET', f'attributes/view/{attribute_id}')
        attribute_r = self._check_json_response(r)
        if not (self.global_pythonify or pythonify) or 'errors' in attribute_r:
            return attribute_r
        a = MISPAttribute()
        a.from_dict(**attribute_r)
        return a

    def add_attribute(self, event: Union[MISPEvent, int, str, UUID], attribute: MISPAttribute, pythonify: bool=False) -> Union[dict, MISPAttribute, MISPShadowAttribute]:
        '''Add an attribute to an existing MISP event
        NOTE MISP 2.4.113+: you can pass a list of attributes.
        In that case, the pythonified response is the following: {'attributes': [MISPAttribute], 'errors': {errors by attributes}}'''
        event_id = self.__get_uuid_or_id_from_abstract_misp(event)
        r = self._prepare_request('POST', f'attributes/add/{event_id}', data=attribute)
        new_attribute = self._check_json_response(r)
        if isinstance(attribute, list):
            # Multiple attributes were passed at once, the handling is totally different
            if not (self.global_pythonify or pythonify):
                return new_attribute
            to_return = {'attributes': []}
            if 'errors' in new_attribute:
                to_return['errors'] = new_attribute['errors']

            for new_attr in new_attribute['Attribute']:
                a = MISPAttribute()
                a.from_dict(**new_attr)
                to_return['attributes'].append(a)
            return to_return

        if ('errors' in new_attribute and new_attribute['errors'][0] == 403
                and new_attribute['errors'][1]['message'] == 'You do not have permission to do that.'):
            # At this point, we assume the user tried to add an attribute on an event they don't own
            # Re-try with a proposal
            return self.add_attribute_proposal(event_id, attribute, pythonify)
        if not (self.global_pythonify or pythonify) or 'errors' in new_attribute:
            return new_attribute
        a = MISPAttribute()
        a.from_dict(**new_attribute)
        return a

    def update_attribute(self, attribute: MISPAttribute, attribute_id: int=None, pythonify: bool=False) -> Union[dict, MISPAttribute, MISPShadowAttribute]:
        '''Update an attribute on a MISP instance'''
        if attribute_id is None:
            aid = self.__get_uuid_or_id_from_abstract_misp(attribute)
        else:
            aid = self.__get_uuid_or_id_from_abstract_misp(attribute_id)
        r = self._prepare_request('POST', f'attributes/edit/{aid}', data=attribute)
        updated_attribute = self._check_json_response(r)
        if 'errors' in updated_attribute:
            if (updated_attribute['errors'][0] == 403
                    and updated_attribute['errors'][1]['message'] == 'You do not have permission to do that.'):
                # At this point, we assume the user tried to update an attribute on an event they don't own
                # Re-try with a proposal
                return self.update_attribute_proposal(aid, attribute, pythonify)
        if not (self.global_pythonify or pythonify) or 'errors' in updated_attribute:
            return updated_attribute
        a = MISPAttribute()
        a.from_dict(**updated_attribute)
        return a

    def delete_attribute(self, attribute: Union[MISPAttribute, int, str, UUID], hard: bool=False) -> dict:
        '''Delete an attribute from a MISP instance'''
        attribute_id = self.__get_uuid_or_id_from_abstract_misp(attribute)
        data = {}
        if hard:
            data['hard'] = 1
        r = self._prepare_request('POST', f'attributes/delete/{attribute_id}', data=data)
        response = self._check_json_response(r)
        if ('errors' in response and response['errors'][0] == 403
                and response['errors'][1]['message'] == 'You do not have permission to do that.'):
            # FIXME: https://github.com/MISP/MISP/issues/4913
            # At this point, we assume the user tried to delete an attribute on an event they don't own
            # Re-try with a proposal
            return self.delete_attribute_proposal(attribute_id)
        return response

    # ## END Attribute ###

    # ## BEGIN Attribute Proposal ###

    def attribute_proposals(self, event: Union[MISPEvent, int, str, UUID]=None, pythonify: bool=False) -> Union[dict, List[MISPShadowAttribute]]:
        if event:
            event_id = self.__get_uuid_or_id_from_abstract_misp(event)
            r = self._prepare_request('GET', f'shadow_attributes/index/{event_id}')
        else:
            r = self._prepare_request('GET', f'shadow_attributes')
        attribute_proposals = self._check_json_response(r)
        if not (self.global_pythonify or pythonify) or 'errors' in attribute_proposals:
            return attribute_proposals
        to_return = []
        for attribute_proposal in attribute_proposals:
            a = MISPShadowAttribute()
            a.from_dict(**attribute_proposal)
            to_return.append(a)
        return to_return

    def get_attribute_proposal(self, proposal: Union[MISPShadowAttribute, int, str, UUID], pythonify: bool=False) -> Union[dict, MISPShadowAttribute]:
        proposal_id = self.__get_uuid_or_id_from_abstract_misp(proposal)
        r = self._prepare_request('GET', f'shadow_attributes/view/{proposal_id}')
        attribute_proposal = self._check_json_response(r)
        if not (self.global_pythonify or pythonify) or 'errors' in attribute_proposal:
            return attribute_proposal
        a = MISPShadowAttribute()
        a.from_dict(**attribute_proposal)
        return a

    # NOTE: the tree following method have a very specific meaning, look at the comments

    def add_attribute_proposal(self, event: Union[MISPEvent, int, str, UUID], attribute: MISPAttribute, pythonify: bool=False) -> Union[dict, MISPShadowAttribute]:
        '''Propose a new attribute in an event'''
        event_id = self.__get_uuid_or_id_from_abstract_misp(event)
        r = self._prepare_request('POST', f'shadow_attributes/add/{event_id}', data=attribute)
        new_attribute_proposal = self._check_json_response(r)
        if not (self.global_pythonify or pythonify) or 'errors' in new_attribute_proposal:
            return new_attribute_proposal
        a = MISPShadowAttribute()
        a.from_dict(**new_attribute_proposal)
        return a

    def update_attribute_proposal(self, initial_attribute: Union[MISPAttribute, int, str, UUID], attribute: MISPAttribute, pythonify: bool=False) -> Union[dict, MISPShadowAttribute]:
        '''Propose a change for an attribute'''
        initial_attribute_id = self.__get_uuid_or_id_from_abstract_misp(initial_attribute)
        r = self._prepare_request('POST', f'shadow_attributes/edit/{initial_attribute_id}', data=attribute)
        update_attribute_proposal = self._check_json_response(r)
        if not (self.global_pythonify or pythonify) or 'errors' in update_attribute_proposal:
            return update_attribute_proposal
        a = MISPShadowAttribute()
        a.from_dict(**update_attribute_proposal)
        return a

    def delete_attribute_proposal(self, attribute: Union[MISPAttribute, int, str, UUID]) -> dict:
        '''Propose the deletion of an attribute'''
        attribute_id = self.__get_uuid_or_id_from_abstract_misp(attribute)
        response = self._prepare_request('POST', f'shadow_attributes/delete/{attribute_id}')
        return self._check_json_response(response)

    # NOTE: You cannot modify an existing proposal, only accept/discard

    def accept_attribute_proposal(self, proposal: Union[MISPShadowAttribute, int, str, UUID]) -> dict:
        '''Accept a proposal'''
        proposal_id = self.__get_uuid_or_id_from_abstract_misp(proposal)
        response = self._prepare_request('POST', f'shadow_attributes/accept/{proposal_id}')
        return self._check_json_response(response)

    def discard_attribute_proposal(self, proposal: Union[MISPShadowAttribute, int, str, UUID]) -> dict:
        '''Discard a proposal'''
        proposal_id = self.__get_uuid_or_id_from_abstract_misp(proposal)
        response = self._prepare_request('POST', f'shadow_attributes/discard/{proposal_id}')
        return self._check_json_response(response)

    # ## END Attribute Proposal ###

    # ## BEGIN Sighting ###

    def sightings(self, misp_entity: AbstractMISP=None, org: Union[MISPOrganisation, int, str, UUID]=None, pythonify: bool=False) -> Union[dict, List[MISPSighting]]:
        """Get the list of sighting related to a MISPEvent or a MISPAttribute (depending on type of misp_entity)"""
        if isinstance(misp_entity, MISPEvent):
            url = 'sightings/listSightings'
            to_post = {'context': 'event', 'id': misp_entity.id}
        elif isinstance(misp_entity, MISPAttribute):
            url = 'sightings/listSightings'
            to_post = {'context': 'attribute', 'id': misp_entity.id}
        else:
            url = 'sightings'
            to_post = {}

        if org is not None:
            org_id = self.__get_uuid_or_id_from_abstract_misp(org)
            to_post['org_id'] = org_id

        r = self._prepare_request('POST', url, data=to_post)
        sightings = self._check_json_response(r)
        if not (self.global_pythonify or pythonify) or 'errors' in sightings:
            return sightings
        to_return = []
        for sighting in sightings:
            s = MISPSighting()
            s.from_dict(**sighting)
            to_return.append(s)
        return to_return

    def add_sighting(self, sighting: MISPSighting, attribute: Union[MISPAttribute, int, str, UUID]=None, pythonify: bool=False) -> Union[dict, MISPSighting]:
        '''Add a new sighting (globally, or to a specific attribute)'''
        if attribute:
            attribute_id = self.__get_uuid_or_id_from_abstract_misp(attribute)
            r = self._prepare_request('POST', f'sightings/add/{attribute_id}', data=sighting)
        else:
            # Either the ID/UUID is in the sighting, or we want to add a sighting on all the attributes with the given value
            r = self._prepare_request('POST', f'sightings/add', data=sighting)
        new_sighting = self._check_json_response(r)
        if not (self.global_pythonify or pythonify) or 'errors' in new_sighting:
            return new_sighting
        s = MISPSighting()
        s.from_dict(**new_sighting)
        return s

    def delete_sighting(self, sighting: Union[MISPSighting, int, str, UUID]) -> dict:
        '''Delete a sighting from a MISP instance'''
        sighting_id = self.__get_uuid_or_id_from_abstract_misp(sighting)
        response = self._prepare_request('POST', f'sightings/delete/{sighting_id}')
        return self._check_json_response(response)

    # ## END Sighting ###

    # ## BEGIN Tags ###

    def tags(self, pythonify: bool=False) -> Union[dict, List[MISPTag]]:
        """Get the list of existing tags."""
        r = self._prepare_request('GET', 'tags')
        tags = self._check_json_response(r)
        if not (self.global_pythonify or pythonify) or 'errors' in tags:
            return tags['Tag']
        to_return = []
        for tag in tags['Tag']:
            t = MISPTag()
            t.from_dict(**tag)
            to_return.append(t)
        return to_return

    def get_tag(self, tag: Union[MISPTag, int, str, UUID], pythonify: bool=False) -> Union[dict, MISPTag]:
        """Get a tag by id."""
        tag_id = self.__get_uuid_or_id_from_abstract_misp(tag)
        r = self._prepare_request('GET', f'tags/view/{tag_id}')
        tag_r = self._check_json_response(r)
        if not (self.global_pythonify or pythonify) or 'errors' in tag_r:
            return tag_r
        t = MISPTag()
        t.from_dict(**tag_r)
        return t

    def add_tag(self, tag: MISPTag, pythonify: bool=False) -> Union[dict, MISPTag]:
        '''Add a new tag on a MISP instance
        Notes:
            * The user calling this method needs the Tag Editor permission
            * It doesn't add a tag to an event, simply create it on a MISP instance.
        '''
        r = self._prepare_request('POST', 'tags/add', data=tag)
        new_tag = self._check_json_response(r)
        if not (self.global_pythonify or pythonify) or 'errors' in new_tag:
            return new_tag
        t = MISPTag()
        t.from_dict(**new_tag)
        return t

    def enable_tag(self, tag: MISPTag, pythonify: bool=False) -> Union[dict, MISPTag]:
        """Enable a tag."""
        tag.hide_tag = False
        return self.update_tag(tag, pythonify=pythonify)

    def disable_tag(self, tag: MISPTag, pythonify: bool=False) -> Union[dict, MISPTag]:
        """Disable a tag."""
        tag.hide_tag = True
        return self.update_tag(tag, pythonify=pythonify)

    def update_tag(self, tag: MISPTag, tag_id: Optional[int]=None, pythonify: bool=False) -> Union[dict, MISPTag]:
        """Edit only the provided parameters of a tag."""
        if tag_id is None:
            tid = self.__get_uuid_or_id_from_abstract_misp(tag)
        else:
            tid = self.__get_uuid_or_id_from_abstract_misp(tag_id)
        r = self._prepare_request('POST', f'tags/edit/{tid}', data=tag)
        updated_tag = self._check_json_response(r)
        if not (self.global_pythonify or pythonify) or 'errors' in updated_tag:
            return updated_tag
        t = MISPTag()
        t.from_dict(**updated_tag)
        return t

    def delete_tag(self, tag: Union[MISPTag, int, str, UUID]) -> dict:
        '''Delete an attribute from a MISP instance'''
        tag_id = self.__get_uuid_or_id_from_abstract_misp(tag)
        response = self._prepare_request('POST', f'tags/delete/{tag_id}')
        return self._check_json_response(response)

    # ## END Tags ###

    # ## BEGIN Taxonomies ###

    def taxonomies(self, pythonify: bool=False) -> Union[dict, List[MISPTaxonomy]]:
        """Get all the taxonomies."""
        r = self._prepare_request('GET', 'taxonomies')
        taxonomies = self._check_json_response(r)
        if not (self.global_pythonify or pythonify) or 'errors' in taxonomies:
            return taxonomies
        to_return = []
        for taxonomy in taxonomies:
            t = MISPTaxonomy()
            t.from_dict(**taxonomy)
            to_return.append(t)
        return to_return

    def get_taxonomy(self, taxonomy: Union[MISPTaxonomy, int, str, UUID], pythonify: bool=False) -> Union[dict, MISPTaxonomy]:
        """Get a taxonomy from a MISP instance."""
        taxonomy_id = self.__get_uuid_or_id_from_abstract_misp(taxonomy)
        r = self._prepare_request('GET', f'taxonomies/view/{taxonomy_id}')
        taxonomy_r = self._check_json_response(r)
        if not (self.global_pythonify or pythonify) or 'errors' in taxonomy_r:
            return taxonomy_r
        t = MISPTaxonomy()
        t.from_dict(**taxonomy_r)
        return t

    def enable_taxonomy(self, taxonomy: Union[MISPTaxonomy, int, str, UUID]) -> dict:
        """Enable a taxonomy."""
        taxonomy_id = self.__get_uuid_or_id_from_abstract_misp(taxonomy)
        response = self._prepare_request('POST', f'taxonomies/enable/{taxonomy_id}')
        return self._check_json_response(response)

    def disable_taxonomy(self, taxonomy: Union[MISPTaxonomy, int, str, UUID]) -> dict:
        """Disable a taxonomy."""
        taxonomy_id = self.__get_uuid_or_id_from_abstract_misp(taxonomy)
        self.disable_taxonomy_tags(taxonomy_id)
        response = self._prepare_request('POST', f'taxonomies/disable/{taxonomy_id}')
        return self._check_json_response(response)

    def disable_taxonomy_tags(self, taxonomy: Union[MISPTaxonomy, int, str, UUID]) -> dict:
        """Disable all the tags of a taxonomy."""
        taxonomy_id = self.__get_uuid_or_id_from_abstract_misp(taxonomy)
        response = self._prepare_request('POST', f'taxonomies/disableTag/{taxonomy_id}')
        return self._check_json_response(response)

    def enable_taxonomy_tags(self, taxonomy: Union[MISPTaxonomy, int, str, UUID]) -> dict:
        """Enable all the tags of a taxonomy.
        NOTE: this automatically done when you call enable_taxonomy."""
        taxonomy_id = self.__get_uuid_or_id_from_abstract_misp(taxonomy)
        t = self.get_taxonomy(taxonomy_id)
        if not t['Taxonomy']['enabled']:
            raise PyMISPError(f"The taxonomy {t['Taxonomy']['name']} is not enabled.")
        url = urljoin(self.root_url, 'taxonomies/addTag/{}'.format(taxonomy_id))
        response = self._prepare_request('POST', url)
        return self._check_json_response(response)

    def update_taxonomies(self) -> dict:
        """Update all the taxonomies."""
        response = self._prepare_request('POST', 'taxonomies/update')
        return self._check_json_response(response)

    # ## END Taxonomies ###

    # ## BEGIN Warninglists ###

    def warninglists(self, pythonify: bool=False) -> Union[dict, List[MISPWarninglist]]:
        """Get all the warninglists."""
        r = self._prepare_request('GET', 'warninglists')
        warninglists = self._check_json_response(r)
        if not (self.global_pythonify or pythonify) or 'errors' in warninglists:
            return warninglists['Warninglists']
        to_return = []
        for warninglist in warninglists['Warninglists']:
            w = MISPWarninglist()
            w.from_dict(**warninglist)
            to_return.append(w)
        return to_return

    def get_warninglist(self, warninglist: Union[MISPWarninglist, int, str, UUID], pythonify: bool=False) -> Union[dict, MISPWarninglist]:
        """Get a warninglist."""
        warninglist_id = self.__get_uuid_or_id_from_abstract_misp(warninglist)
        r = self._prepare_request('GET', f'warninglists/view/{warninglist_id}')
        wl = self._check_json_response(r)
        if not (self.global_pythonify or pythonify) or 'errors' in wl:
            return wl
        w = MISPWarninglist()
        w.from_dict(**wl)
        return w

    def toggle_warninglist(self, warninglist_id: Optional[Union[str, int, List[int]]]=None, warninglist_name: Optional[Union[str, List[str]]]=None, force_enable: bool=False) -> dict:
        '''Toggle (enable/disable) the status of a warninglist by ID.
        :param warninglist_id: ID of the WarningList
        :param force_enable: Force the warning list in the enabled state (does nothing is already enabled)
        '''
        if warninglist_id is None and warninglist_name is None:
            raise PyMISPError('Either warninglist_id or warninglist_name is required.')
        query: Dict[str, Union[List[str], List[int], bool]] = {}
        if warninglist_id is not None:
            if isinstance(warninglist_id, list):
                query['id'] = warninglist_id
            else:
                query['id'] = [warninglist_id]  # type: ignore
        if warninglist_name is not None:
            if isinstance(warninglist_name, list):
                query['name'] = warninglist_name
            else:
                query['name'] = [warninglist_name]
        if force_enable:
            query['enabled'] = force_enable
        response = self._prepare_request('POST', 'warninglists/toggleEnable', data=query)
        return self._check_json_response(response)

    def enable_warninglist(self, warninglist: Union[MISPWarninglist, int, str, UUID]) -> dict:
        """Enable a warninglist."""
        warninglist_id = self.__get_uuid_or_id_from_abstract_misp(warninglist)
        return self.toggle_warninglist(warninglist_id=warninglist_id, force_enable=True)

    def disable_warninglist(self, warninglist: Union[MISPWarninglist, int, str, UUID]) -> dict:
        """Disable a warninglist."""
        warninglist_id = self.__get_uuid_or_id_from_abstract_misp(warninglist)
        return self.toggle_warninglist(warninglist_id=warninglist_id, force_enable=False)

    def values_in_warninglist(self, value: list) -> dict:
        """Check if IOC values are in warninglist"""
        response = self._prepare_request('POST', 'warninglists/checkValue', data=value)
        return self._check_json_response(response)

    def update_warninglists(self) -> dict:
        """Update all the warninglists."""
        response = self._prepare_request('POST', 'warninglists/update')
        return self._check_json_response(response)

    # ## END Warninglists ###

    # ## BEGIN Noticelist ###

    def noticelists(self, pythonify: bool=False) -> Union[dict, List[MISPNoticelist]]:
        """Get all the noticelists."""
        r = self._prepare_request('GET', 'noticelists')
        noticelists = self._check_json_response(r)
        if not (self.global_pythonify or pythonify) or 'errors' in noticelists:
            return noticelists
        to_return = []
        for noticelist in noticelists:
            n = MISPNoticelist()
            n.from_dict(**noticelist)
            to_return.append(n)
        return to_return

    def get_noticelist(self, noticelist: Union[MISPNoticelist, int, str, UUID], pythonify: bool=False) -> Union[dict, MISPNoticelist]:
        """Get a noticelist by id."""
        noticelist_id = self.__get_uuid_or_id_from_abstract_misp(noticelist)
        r = self._prepare_request('GET', f'noticelists/view/{noticelist_id}')
        noticelist_j = self._check_json_response(r)
        if not (self.global_pythonify or pythonify) or 'errors' in noticelist_j:
            return noticelist_j
        n = MISPNoticelist()
        n.from_dict(**noticelist_j)
        return n

    def enable_noticelist(self, noticelist: Union[MISPNoticelist, int, str, UUID]) -> dict:
        """Enable a noticelist by id."""
        # FIXME: https://github.com/MISP/MISP/issues/4856
        # response = self._prepare_request('POST', f'noticelists/enable/{noticelist_id}')
        noticelist_id = self.__get_uuid_or_id_from_abstract_misp(noticelist)
        response = self._prepare_request('POST', f'noticelists/enableNoticelist/{noticelist_id}/true')
        return self._check_json_response(response)

    def disable_noticelist(self, noticelist: Union[MISPNoticelist, int, str, UUID]) -> dict:
        """Disable a noticelist by id."""
        # FIXME: https://github.com/MISP/MISP/issues/4856
        # response = self._prepare_request('POST', f'noticelists/disable/{noticelist_id}')
        noticelist_id = self.__get_uuid_or_id_from_abstract_misp(noticelist)
        response = self._prepare_request('POST', f'noticelists/enableNoticelist/{noticelist_id}')
        return self._check_json_response(response)

    def update_noticelists(self) -> dict:
        """Update all the noticelists."""
        response = self._prepare_request('POST', 'noticelists/update')
        return self._check_json_response(response)

    # ## END Noticelist ###

    # ## BEGIN Galaxy ###

    def galaxies(self, pythonify: bool=False) -> Union[dict, List[MISPGalaxy]]:
        """Get all the galaxies."""
        r = self._prepare_request('GET', 'galaxies')
        galaxies = self._check_json_response(r)
        if not (self.global_pythonify or pythonify) or 'errors' in galaxies:
            return galaxies
        to_return = []
        for galaxy in galaxies:
            g = MISPGalaxy()
            g.from_dict(**galaxy)
            to_return.append(g)
        return to_return

    def get_galaxy(self, galaxy: Union[MISPGalaxy, int, str, UUID], pythonify: bool=False) -> Union[dict, MISPGalaxy]:
        """Get a galaxy by id."""
        galaxy_id = self.__get_uuid_or_id_from_abstract_misp(galaxy)
        r = self._prepare_request('GET', f'galaxies/view/{galaxy_id}')
        galaxy_j = self._check_json_response(r)
        if not (self.global_pythonify or pythonify) or 'errors' in galaxy_j:
            return galaxy_j
        g = MISPGalaxy()
        g.from_dict(**galaxy_j)
        return g

    def update_galaxies(self) -> dict:
        """Update all the galaxies."""
        response = self._prepare_request('POST', 'galaxies/update')
        return self._check_json_response(response)

    # ## END Galaxy ###

    # ## BEGIN Feed ###

    def feeds(self, pythonify: bool=False) -> Union[dict, List[MISPFeed]]:
        """Get the list of existing feeds."""
        r = self._prepare_request('GET', 'feeds')
        feeds = self._check_json_response(r)
        if not (self.global_pythonify or pythonify) or 'errors' in feeds:
            return feeds
        to_return = []
        for feed in feeds:
            f = MISPFeed()
            f.from_dict(**feed)
            to_return.append(f)
        return to_return

    def get_feed(self, feed: Union[MISPFeed, int, str, UUID], pythonify: bool=False) -> Union[dict, MISPFeed]:
        """Get a feed by id."""
        feed_id = self.__get_uuid_or_id_from_abstract_misp(feed)
        r = self._prepare_request('GET', f'feeds/view/{feed_id}')
        feed_j = self._check_json_response(r)
        if not (self.global_pythonify or pythonify) or 'errors' in feed_j:
            return feed_j
        f = MISPFeed()
        f.from_dict(**feed_j)
        return f

    def add_feed(self, feed: MISPFeed, pythonify: bool=False) -> Union[dict, MISPFeed]:
        '''Add a new feed on a MISP instance'''
        # FIXME: https://github.com/MISP/MISP/issues/4834
        r = self._prepare_request('POST', 'feeds/add', data={'Feed': feed})
        new_feed = self._check_json_response(r)
        if not (self.global_pythonify or pythonify) or 'errors' in new_feed:
            return new_feed
        f = MISPFeed()
        f.from_dict(**new_feed)
        return f

    def enable_feed(self, feed: Union[MISPFeed, int, str, UUID], pythonify: bool=False) -> Union[dict, MISPFeed]:
        '''Enable a feed (fetching it will create event(s)'''
        if not isinstance(feed, MISPFeed):
            feed_id = self.__get_uuid_or_id_from_abstract_misp(feed)  # In case we have a UUID
            f = MISPFeed()
            f.id = feed_id
            f.enabled = True
        return self.update_feed(feed=f, pythonify=pythonify)

    def disable_feed(self, feed: Union[MISPFeed, int, str, UUID], pythonify: bool=False) -> Union[dict, MISPFeed]:
        '''Disable a feed'''
        if not isinstance(feed, MISPFeed):
            feed_id = self.__get_uuid_or_id_from_abstract_misp(feed)  # In case we have a UUID
            f = MISPFeed()
            f.id = feed_id
            f.enabled = False
        return self.update_feed(feed=f, pythonify=pythonify)

    def enable_feed_cache(self, feed: Union[MISPFeed, int, str, UUID], pythonify: bool=False) -> Union[dict, MISPFeed]:
        '''Enable the caching of a feed'''
        if not isinstance(feed, MISPFeed):
            feed_id = self.__get_uuid_or_id_from_abstract_misp(feed)  # In case we have a UUID
            f = MISPFeed()
            f.id = feed_id
            f.caching_enabled = True
        return self.update_feed(feed=f, pythonify=pythonify)

    def disable_feed_cache(self, feed: Union[MISPFeed, int, str, UUID], pythonify: bool=False) -> Union[dict, MISPFeed]:
        '''Disable the caching of a feed'''
        if not isinstance(feed, MISPFeed):
            feed_id = self.__get_uuid_or_id_from_abstract_misp(feed)  # In case we have a UUID
            f = MISPFeed()
            f.id = feed_id
            f.caching_enabled = False
        return self.update_feed(feed=f, pythonify=pythonify)

    def update_feed(self, feed: MISPFeed, feed_id: int=None, pythonify: bool=False) -> Union[dict, MISPFeed]:
        '''Update a feed on a MISP instance'''
        if feed_id is None:
            fid = self.__get_uuid_or_id_from_abstract_misp(feed)
        else:
            fid = self.__get_uuid_or_id_from_abstract_misp(feed_id)
        # FIXME: https://github.com/MISP/MISP/issues/4834
        r = self._prepare_request('POST', f'feeds/edit/{fid}', data={'Feed': feed})
        updated_feed = self._check_json_response(r)
        if not (self.global_pythonify or pythonify) or 'errors' in updated_feed:
            return updated_feed
        f = MISPFeed()
        f.from_dict(**updated_feed)
        return f

    def delete_feed(self, feed: Union[MISPFeed, int, str, UUID]) -> dict:
        '''Delete a feed from a MISP instance'''
        feed_id = self.__get_uuid_or_id_from_abstract_misp(feed)
        response = self._prepare_request('POST', f'feeds/delete/{feed_id}')
        return self._check_json_response(response)

    def fetch_feed(self, feed: Union[MISPFeed, int, str, UUID]) -> dict:
        """Fetch one single feed"""
        feed_id = self.__get_uuid_or_id_from_abstract_misp(feed)
        response = self._prepare_request('GET', f'feeds/fetchFromFeed/{feed_id}')
        return self._check_json_response(response)

    def cache_all_feeds(self) -> dict:
        """ Cache all the feeds"""
        response = self._prepare_request('GET', 'feeds/cacheFeeds/all')
        return self._check_json_response(response)

    def cache_feed(self, feed: Union[MISPFeed, int, str, UUID]) -> dict:
        """Cache a specific feed"""
        feed_id = self.__get_uuid_or_id_from_abstract_misp(feed)
        response = self._prepare_request('GET', f'feeds/cacheFeeds/{feed_id}')
        return self._check_json_response(response)

    def cache_freetext_feeds(self) -> dict:
        """Cache all the freetext feeds"""
        response = self._prepare_request('GET', 'feeds/cacheFeeds/freetext')
        return self._check_json_response(response)

    def cache_misp_feeds(self) -> dict:
        """Cache all the MISP feeds"""
        response = self._prepare_request('GET', 'feeds/cacheFeeds/misp')
        return self._check_json_response(response)

    def compare_feeds(self) -> dict:
        """Generate the comparison matrix for all the MISP feeds"""
        response = self._prepare_request('GET', 'feeds/compareFeeds')
        return self._check_json_response(response)

    # ## END Feed ###

    # ## BEGIN Server ###

    def servers(self, pythonify: bool=False) -> Union[dict, List[MISPServer]]:
        """Get the existing servers the MISP instance can synchronise with"""
        r = self._prepare_request('GET', 'servers')
        servers = self._check_json_response(r)
        if not (self.global_pythonify or pythonify) or 'errors' in servers:
            return servers
        to_return = []
        for server in servers:
            s = MISPServer()
            s.from_dict(**server)
            to_return.append(s)
        return to_return

    def get_sync_config(self, pythonify: bool=False) -> Union[dict, MISPServer]:
        '''WARNING: This method only works if the user calling it is a sync user'''
        r = self._prepare_request('GET', 'servers/createSync')
        server = self._check_json_response(r)
        if not (self.global_pythonify or pythonify) or 'errors' in server:
            return server
        s = MISPServer()
        s.from_dict(**server)
        return s

    def import_server(self, server: MISPServer, pythonify: bool=False) -> Union[dict, MISPServer]:
        """Import a sync server config received from get_sync_config"""
        r = self._prepare_request('POST', f'servers/import', data=server)
        server_j = self._check_json_response(r)
        if not (self.global_pythonify or pythonify) or 'errors' in server_j:
            return server_j
        s = MISPServer()
        s.from_dict(**server_j)
        return s

    def add_server(self, server: MISPServer, pythonify: bool=False) -> Union[dict, MISPServer]:
        """Add a server to synchronise with.
        Note: You probably want to use ExpandedPyMISP.get_sync_config and ExpandedPyMISP.import_server instead"""
        r = self._prepare_request('POST', f'servers/add', data=server)
        server_j = self._check_json_response(r)
        if not (self.global_pythonify or pythonify) or 'errors' in server_j:
            return server_j
        s = MISPServer()
        s.from_dict(**server_j)
        return s

    def update_server(self, server: MISPServer, server_id: int=None, pythonify: bool=False) -> Union[dict, MISPServer]:
        '''Update a server to synchronise with'''
        if server_id is None:
            sid = self.__get_uuid_or_id_from_abstract_misp(server)
        else:
            sid = self.__get_uuid_or_id_from_abstract_misp(server_id)
        r = self._prepare_request('POST', f'servers/edit/{sid}', data=server)
        updated_server = self._check_json_response(r)
        if not (self.global_pythonify or pythonify) or 'errors' in updated_server:
            return updated_server
        s = MISPServer()
        s.from_dict(**updated_server)
        return s

    def delete_server(self, server: Union[MISPServer, int, str, UUID]) -> dict:
        '''Delete a sync server'''
        server_id = self.__get_uuid_or_id_from_abstract_misp(server)
        response = self._prepare_request('POST', f'servers/delete/{server_id}')
        return self._check_json_response(response)

    def server_pull(self, server: Union[MISPServer, int, str, UUID], event: Optional[Union[MISPEvent, int, str, UUID]]=None) -> dict:
        '''Initialize a pull from a sync server'''
        server_id = self.__get_uuid_or_id_from_abstract_misp(server)
        if event:
            event_id = self.__get_uuid_or_id_from_abstract_misp(event)
            url = f'servers/pull/{server_id}/{event_id}'
        else:
            url = f'servers/pull/{server_id}'
        response = self._prepare_request('GET', url)
        # FIXME: can we pythonify?
        return self._check_json_response(response)

    def server_push(self, server: Union[MISPServer, int, str, UUID], event: Optional[Union[MISPEvent, int, str, UUID]]=None) -> dict:
        '''Initialize a push to a sync server'''
        server_id = self.__get_uuid_or_id_from_abstract_misp(server)
        if event:
            event_id = self.__get_uuid_or_id_from_abstract_misp(event)
            url = f'servers/push/{server_id}/{event_id}'
        else:
            url = f'servers/push/{server_id}'
        response = self._prepare_request('GET', url)
        # FIXME: can we pythonify?
        return self._check_json_response(response)

    def test_server(self, server: Union[MISPServer, int, str, UUID]) -> dict:
        server_id = self.__get_uuid_or_id_from_abstract_misp(server)
        response = self._prepare_request('POST', f'servers/testConnection/{server_id}')
        return self._check_json_response(response)

    # ## END Server ###

    # ## BEGIN Sharing group ###

    def sharing_groups(self, pythonify: bool=False) -> Union[dict, List[MISPSharingGroup]]:
        """Get the existing sharing groups"""
        r = self._prepare_request('GET', 'sharing_groups')
        sharing_groups = self._check_json_response(r)
        if not (self.global_pythonify or pythonify) or 'errors' in sharing_groups:
            return sharing_groups
        to_return = []
        for sharing_group in sharing_groups:
            s = MISPSharingGroup()
            s.from_dict(**sharing_group)
            to_return.append(s)
        return to_return

    def add_sharing_group(self, sharing_group: MISPSharingGroup, pythonify: bool=False) -> Union[dict, MISPSharingGroup]:
        """Add a new sharing group"""
        r = self._prepare_request('POST', f'sharing_groups/add', data=sharing_group)
        sharing_group_j = self._check_json_response(r)
        if not (self.global_pythonify or pythonify) or 'errors' in sharing_group_j:
            return sharing_group_j
        s = MISPSharingGroup()
        s.from_dict(**sharing_group_j)
        return s

    def delete_sharing_group(self, sharing_group: Union[MISPSharingGroup, int, str, UUID]) -> dict:
        """Delete a sharing group"""
        sharing_group_id = self.__get_uuid_or_id_from_abstract_misp(sharing_group)
        response = self._prepare_request('POST', f'sharing_groups/delete/{sharing_group_id}')
        return self._check_json_response(response)

    def add_org_to_sharing_group(self, sharing_group: Union[MISPSharingGroup, int, str, UUID],
                                 organisation: Union[MISPOrganisation, int, str, UUID], extend: bool=False) -> dict:
        '''Add an organisation to a sharing group.
        :sharing_group: Sharing group's local instance ID, or Sharing group's global UUID
        :organisation: Organisation's local instance ID, or Organisation's global UUID, or Organisation's name as known to the curent instance
        :extend: Allow the organisation to extend the group
        '''
        sharing_group_id = self.__get_uuid_or_id_from_abstract_misp(sharing_group)
        organisation_id = self.__get_uuid_or_id_from_abstract_misp(organisation)
        to_jsonify = {'sg_id': sharing_group_id, 'org_id': organisation_id, 'extend': extend}
        response = self._prepare_request('POST', 'sharingGroups/addOrg', data=to_jsonify)
        return self._check_json_response(response)

    def remove_org_from_sharing_group(self, sharing_group: Union[MISPSharingGroup, int, str, UUID],
                                      organisation: Union[MISPOrganisation, int, str, UUID]) -> dict:
        '''Remove an organisation from a sharing group.
        :sharing_group: Sharing group's local instance ID, or Sharing group's global UUID
        :organisation: Organisation's local instance ID, or Organisation's global UUID, or Organisation's name as known to the curent instance
        '''
        sharing_group_id = self.__get_uuid_or_id_from_abstract_misp(sharing_group)
        organisation_id = self.__get_uuid_or_id_from_abstract_misp(organisation)
        to_jsonify = {'sg_id': sharing_group_id, 'org_id': organisation_id}
        response = self._prepare_request('POST', 'sharingGroups/removeOrg', data=to_jsonify)
        return self._check_json_response(response)

    def add_server_to_sharing_group(self, sharing_group: Union[MISPSharingGroup, int, str, UUID],
                                    server: Union[MISPServer, int, str, UUID], all_orgs: bool=False) -> dict:
        '''Add a server to a sharing group.
        :sharing_group: Sharing group's local instance ID, or Sharing group's global UUID
        :server: Server's local instance ID, or URL of the Server, or Server's name as known to the curent instance
        :all_orgs: Add all the organisations of the server to the group
        '''
        sharing_group_id = self.__get_uuid_or_id_from_abstract_misp(sharing_group)
        server_id = self.__get_uuid_or_id_from_abstract_misp(server)
        to_jsonify = {'sg_id': sharing_group_id, 'server_id': server_id, 'all_orgs': all_orgs}
        response = self._prepare_request('POST', 'sharingGroups/addServer', data=to_jsonify)
        return self._check_json_response(response)

    def remove_server_from_sharing_group(self, sharing_group: Union[MISPSharingGroup, int, str, UUID],
                                         server: Union[MISPServer, int, str, UUID]) -> dict:
        '''Remove a server from a sharing group.
        :sharing_group: Sharing group's local instance ID, or Sharing group's global UUID
        :server: Server's local instance ID, or URL of the Server, or Server's name as known to the curent instance
        '''
        sharing_group_id = self.__get_uuid_or_id_from_abstract_misp(sharing_group)
        server_id = self.__get_uuid_or_id_from_abstract_misp(server)
        to_jsonify = {'sg_id': sharing_group_id, 'server_id': server_id}
        response = self._prepare_request('POST', 'sharingGroups/removeServer', data=to_jsonify)
        return self._check_json_response(response)

    # ## END Sharing groups ###

    # ## BEGIN Organisation ###

    def organisations(self, scope="local", pythonify: bool=False) -> Union[dict, List[MISPOrganisation]]:
        """Get all the organisations."""
        r = self._prepare_request('GET', f'organisations/index/scope:{scope}')
        organisations = self._check_json_response(r)
        if not (self.global_pythonify or pythonify) or 'errors' in organisations:
            return organisations
        to_return = []
        for organisation in organisations:
            o = MISPOrganisation()
            o.from_dict(**organisation)
            to_return.append(o)
        return to_return

    def get_organisation(self, organisation: Union[MISPOrganisation, int, str, UUID], pythonify: bool=False) -> Union[dict, MISPOrganisation]:
        '''Get an organisation.'''
        organisation_id = self.__get_uuid_or_id_from_abstract_misp(organisation)
        r = self._prepare_request('GET', f'organisations/view/{organisation_id}')
        organisation_j = self._check_json_response(r)
        if not (self.global_pythonify or pythonify) or 'errors' in organisation_j:
            return organisation_j
        o = MISPOrganisation()
        o.from_dict(**organisation_j)
        return o

    def add_organisation(self, organisation: MISPOrganisation, pythonify: bool=False) -> Union[dict, MISPOrganisation]:
        '''Add an organisation'''
        r = self._prepare_request('POST', f'admin/organisations/add', data=organisation)
        new_organisation = self._check_json_response(r)
        if not (self.global_pythonify or pythonify) or 'errors' in new_organisation:
            return new_organisation
        o = MISPOrganisation()
        o.from_dict(**new_organisation)
        return o

    def update_organisation(self, organisation: MISPOrganisation, organisation_id: int=None, pythonify: bool=False) -> Union[dict, MISPOrganisation]:
        '''Update an organisation'''
        if organisation_id is None:
            oid = self.__get_uuid_or_id_from_abstract_misp(organisation)
        else:
            oid = self.__get_uuid_or_id_from_abstract_misp(organisation_id)
        r = self._prepare_request('POST', f'admin/organisations/edit/{oid}', data=organisation)
        updated_organisation = self._check_json_response(r)
        if not (self.global_pythonify or pythonify) or 'errors' in updated_organisation:
            return updated_organisation
        o = MISPOrganisation()
        o.from_dict(**organisation)
        return o

    def delete_organisation(self, organisation: Union[MISPOrganisation, int, str, UUID]) -> dict:
        '''Delete an organisation'''
        # NOTE: MISP in inconsistent and currently require "delete" in the path and doesn't support HTTP DELETE
        organisation_id = self.__get_uuid_or_id_from_abstract_misp(organisation)
        response = self._prepare_request('POST', f'admin/organisations/delete/{organisation_id}')
        return self._check_json_response(response)

    # ## END Organisation ###

    # ## BEGIN User ###

    def users(self, pythonify: bool=False) -> Union[dict, List[MISPUser]]:
        """Get all the users."""
        r = self._prepare_request('GET', 'admin/users')
        users = self._check_json_response(r)
        if not (self.global_pythonify or pythonify) or 'errors' in users:
            return users
        to_return = []
        for user in users:
            u = MISPUser()
            u.from_dict(**user)
            to_return.append(u)
        return to_return

    def get_user(self, user: Union[MISPUser, int, str, UUID]='me', pythonify: bool=False, expanded: bool=False) -> Union[dict, MISPUser, Tuple[MISPUser, MISPRole, List[MISPUserSetting]]]:
        '''Get a user. `me` means the owner of the API key doing the query.
        expanded also returns a MISPRole and a MISPUserSetting'''
        user_id = self.__get_uuid_or_id_from_abstract_misp(user)
        r = self._prepare_request('GET', f'users/view/{user_id}')
        user_j = self._check_json_response(r)
        if not (self.global_pythonify or pythonify) or 'errors' in user_j:
            return user_j
        u = MISPUser()
        u.from_dict(**user_j)
        if not expanded:
            return u
        else:
            role = MISPRole()
            role.from_dict(**user_j['Role'])
            usersettings = []
            if user_j['UserSetting']:
                for name, value in user_j['UserSetting'].items():
                    us = MISPUserSetting()
                    us.from_dict(**{'name': name, 'value': value})
                    usersettings.append(us)
            return u, role, usersettings

    def add_user(self, user: MISPUser, pythonify: bool=False) -> Union[dict, MISPUser]:
        '''Add a new user'''
        r = self._prepare_request('POST', f'admin/users/add', data=user)
        user_j = self._check_json_response(r)
        if not (self.global_pythonify or pythonify) or 'errors' in user_j:
            return user_j
        u = MISPUser()
        u.from_dict(**user_j)
        return u

    def update_user(self, user: MISPUser, user_id: int=None, pythonify: bool=False) -> Union[dict, MISPUser]:
        '''Update an event on a MISP instance'''
        if user_id is None:
            uid = self.__get_uuid_or_id_from_abstract_misp(user)
        else:
            uid = self.__get_uuid_or_id_from_abstract_misp(user_id)
        url = f'users/edit/{uid}'
        if self._current_role.perm_admin or self._current_role.perm_site_admin:
            url = f'admin/{url}'
        r = self._prepare_request('POST', url, data=user)
        updated_user = self._check_json_response(r)
        if not (self.global_pythonify or pythonify) or 'errors' in updated_user:
            return updated_user
        e = MISPUser()
        e.from_dict(**updated_user)
        return e

    def delete_user(self, user: Union[MISPUser, int, str, UUID]) -> dict:
        '''Delete a user'''
        # NOTE: MISP in inconsistent and currently require "delete" in the path and doesn't support HTTP DELETE
        user_id = self.__get_uuid_or_id_from_abstract_misp(user)
        response = self._prepare_request('POST', f'admin/users/delete/{user_id}')
        return self._check_json_response(response)

    def change_user_password(self, new_password: str, user: Optional[Union[MISPUser, int, str, UUID]]=None) -> dict:
        response = self._prepare_request('POST', f'users/change_pw', data={'password': new_password})
        return self._check_json_response(response)

    # ## END User ###

    # ## BEGIN Role ###

    def roles(self, pythonify: bool=False) -> Union[dict, List[MISPRole]]:
        """Get the existing roles"""
        r = self._prepare_request('GET', 'roles')
        roles = self._check_json_response(r)
        if not (self.global_pythonify or pythonify) or 'errors' in roles:
            return roles
        to_return = []
        for role in roles:
            nr = MISPRole()
            nr.from_dict(**role)
            to_return.append(nr)
        return to_return

    def set_default_role(self, role: Union[MISPRole, int, str, UUID]) -> dict:
        role_id = self.__get_uuid_or_id_from_abstract_misp(role)
        url = urljoin(self.root_url, f'/admin/roles/set_default/{role_id}')
        response = self._prepare_request('POST', url)
        return self._check_json_response(response)

    # ## END Role ###

    # ## BEGIN Search methods ###

    def search(self, controller: str='events', return_format: str='json',
               limit: Optional[int]=None, page: Optional[int]=None,
               value: Optional[SearchParameterTypes]=None,
               type_attribute: Optional[SearchParameterTypes]=None,
               category: Optional[SearchParameterTypes]=None,
               org: Optional[SearchParameterTypes]=None,
               tags: Optional[SearchParameterTypes]=None,
               quick_filter: Optional[str]=None, quickFilter: Optional[str]=None,
               date_from: Optional[Union[datetime, date, int, str, float, None]]=None,
               date_to: Optional[Union[datetime, date, int, str, float, None]]=None,
               eventid: Optional[SearchType]=None,
               with_attachments: Optional[bool]=None, withAttachments: Optional[bool]=None,
               metadata: Optional[bool]=None,
               uuid: Optional[str]=None,
               publish_timestamp: Optional[Union[Union[datetime, date, int, str, float, None],
                                           Tuple[Union[datetime, date, int, str, float, None],
                                                 Union[datetime, date, int, str, float, None]]
                                                 ]]=None,
               last: Optional[Union[Union[datetime, date, int, str, float, None],
                              Tuple[Union[datetime, date, int, str, float, None],
                                    Union[datetime, date, int, str, float, None]]
                                    ]]=None,
               timestamp: Optional[Union[Union[datetime, date, int, str, float, None],
                                   Tuple[Union[datetime, date, int, str, float, None],
                                         Union[datetime, date, int, str, float, None]]
                                         ]]=None,
               published: Optional[bool]=None,
               enforce_warninglist: Optional[bool]=None, enforceWarninglist: Optional[bool]=None,
               to_ids: Optional[Union[ToIDSType, List[ToIDSType]]]=None,
               deleted: Optional[str]=None,
               include_event_uuid: Optional[bool]=None, includeEventUuid: Optional[bool]=None,
               include_event_tags: Optional[bool]=None, includeEventTags: Optional[bool]=None,
               event_timestamp: Optional[Union[datetime, date, int, str, float, None]]=None,
               sg_reference_only: Optional[bool]=None,
               eventinfo: Optional[str]=None,
               searchall: Optional[bool]=None,
               requested_attributes: Optional[str]=None,
               include_context: Optional[bool]=None, includeContext: Optional[bool]=None,
               headerless: Optional[bool]=None,
               include_sightings: Optional[bool]=None, includeSightings: Optional[bool]=None,
               include_correlations: Optional[bool]=None, includeCorrelations: Optional[bool]=None,
               include_decay_score: Optional[bool] = None, includeDecayScore: Optional[bool] = None,
               pythonify: Optional[bool]=False,
               **kwargs) -> Union[dict, str, List[Union[MISPEvent, MISPAttribute]]]:
        '''Search in the MISP instance

        :param return_format: Set the return format of the search (Currently supported: json, xml, openioc, suricata, snort - more formats are being moved to restSearch with the goal being that all searches happen through this API). Can be passed as the first parameter after restSearch or via the JSON payload.
        :param limit: Limit the number of results returned, depending on the scope (for example 10 attributes or 10 full events).
        :param page: If a limit is set, sets the page to be returned. page 3, limit 100 will return records 201->300).
        :param value: Search for the given value in the attributes' value field.
        :param type_attribute: The attribute type, any valid MISP attribute type is accepted.
        :param category: The attribute category, any valid MISP attribute category is accepted.
        :param org: Search by the creator organisation by supplying the organisation identifier.
        :param tags: Tags to search or to exclude. You can pass a list, or the output of `build_complex_query`
        :param quick_filter: The string passed to this field will ignore all of the other arguments. MISP will return an xml / json (depending on the header sent) of all events that have a sub-string match on value in the event info, event orgc, or any of the attribute value1 / value2 fields, or in the attribute comment.
        :param date_from: Events with the date set to a date after the one specified. This filter will use the date of the event.
        :param date_to: Events with the date set to a date before the one specified. This filter will use the date of the event.
        :param eventid: The events that should be included / excluded from the search
        :param with_attachments: If set, encodes the attachments / zipped malware samples as base64 in the data field within each attribute
        :param metadata: Only the metadata (event, tags, relations) is returned, attributes and proposals are omitted.
        :param uuid: Restrict the results by uuid.
        :param publish_timestamp: Restrict the results by the last publish timestamp (newer than).
        :param timestamp: Restrict the results by the timestamp (last edit). Any event with a timestamp newer than the given timestamp will be returned. In case you are dealing with /attributes as scope, the attribute's timestamp will be used for the lookup.
        :param published: Set whether published or unpublished events should be returned. Do not set the parameter if you want both.
        :param enforce_warninglist: Remove any attributes from the result that would cause a hit on a warninglist entry.
        :param to_ids: By default all attributes are returned that match the other filter parameters, irregardless of their to_ids setting. To restrict the returned data set to to_ids only attributes set this parameter to 1. 0 for the ones with to_ids set to False.
        :param deleted: If this parameter is set to 1, it will return soft-deleted attributes along with active ones. By using "only" as a parameter it will limit the returned data set to soft-deleted data only.
        :param include_event_uuid: Instead of just including the event ID, also include the event UUID in each of the attributes.
        :param include_event_tags: Include the event level tags in each of the attributes.
        :param event_timestamp: Only return attributes from events that have received a modification after the given timestamp.
        :param sg_reference_only: If this flag is set, sharing group objects will not be included, instead only the sharing group ID is set.
        :param eventinfo: Filter on the event's info field.
        :param searchall: Search for a full or a substring (delimited by % for substrings) in the event info, event tags, attribute tags, attribute values or attribute comment fields.
        :param requested_attributes: [CSV only] Select the fields that you wish to include in the CSV export. By setting event level fields additionally, includeContext is not required to get event metadata.
        :param include_context: [Attribute only] Include the event data with each attribute. [CSV output] Add event level metadata in every line of the CSV.
        :param headerless: [CSV Only] The CSV created when this setting is set to true will not contain the header row.
        :param include_sightings: [JSON Only - Attribute] Include the sightings of the matching attributes.
        :param include_decay_score: Include the decay score at attribute level.
        :param include_correlations: [JSON Only - attribute] Include the correlations of the matching attributes.
        :param pythonify: Returns a list of PyMISP Objects instead of the plain json output. Warning: it might use a lot of RAM

        Deprecated:

        :param quickFilter: synponym for quick_filter
        :param withAttachments: synonym for with_attachments
        :param last: synonym for publish_timestamp
        :param enforceWarninglist: synonym for enforce_warninglist
        :param includeEventUuid: synonym for include_event_uuid
        :param includeEventTags: synonym for include_event_tags
        :param includeContext: synonym for include_context

        '''

        return_formats = ['openioc', 'json', 'xml', 'suricata', 'snort', 'text', 'rpz', 'csv', 'cache', 'stix', 'stix2', 'yara', 'yara-json', 'attack', 'attack-sightings']

        if controller not in ['events', 'attributes', 'objects', 'sightings']:
            raise ValueError('controller has to be in {}'.format(', '.join(['events', 'attributes', 'objects'])))

        # Deprecated stuff / synonyms
        if quickFilter is not None:
            quick_filter = quickFilter
        if withAttachments is not None:
            with_attachments = withAttachments
        if last is not None:
            publish_timestamp = last
        if enforceWarninglist is not None:
            enforce_warninglist = enforceWarninglist
        if includeEventUuid is not None:
            include_event_uuid = includeEventUuid
        if includeEventTags is not None:
            include_event_tags = includeEventTags
        if includeContext is not None:
            include_context = includeContext
        if includeDecayScore is not None:
            include_decay_score = includeDecayScore
        if includeCorrelations is not None:
            include_correlations = includeCorrelations
        if includeSightings is not None:
            include_sightings = includeSightings
        # Add all the parameters in kwargs are aimed at modules, or other 3rd party components, and cannot be sanitized.
        # They are passed as-is.
        query = kwargs

        if return_format not in return_formats:
            raise ValueError('return_format has to be in {}'.format(', '.join(return_formats)))
        query['returnFormat'] = return_format

        query['page'] = page
        query['limit'] = limit
        query['value'] = value
        query['type'] = type_attribute
        query['category'] = category
        query['org'] = org
        query['tags'] = tags
        query['quickFilter'] = quick_filter
        query['from'] = self._make_timestamp(date_from)
        query['to'] = self._make_timestamp(date_to)
        query['eventid'] = eventid
        query['withAttachments'] = self._make_misp_bool(with_attachments)
        query['metadata'] = self._make_misp_bool(metadata)
        query['uuid'] = uuid
        if publish_timestamp is not None:
            if isinstance(publish_timestamp, (list, tuple)):
                query['publish_timestamp'] = (self._make_timestamp(publish_timestamp[0]), self._make_timestamp(publish_timestamp[1]))
            else:
                query['publish_timestamp'] = self._make_timestamp(publish_timestamp)
        if timestamp is not None:
            if isinstance(timestamp, (list, tuple)):
                query['timestamp'] = (self._make_timestamp(timestamp[0]), self._make_timestamp(timestamp[1]))
            else:
                query['timestamp'] = self._make_timestamp(timestamp)
        query['published'] = published
        query['enforceWarninglist'] = self._make_misp_bool(enforce_warninglist)
        if to_ids is not None:
            if to_ids not in [0, 1, '0', '1']:
                raise ValueError('to_ids has to be in 0 or 1')
            query['to_ids'] = to_ids
        query['deleted'] = deleted
        query['includeEventUuid'] = self._make_misp_bool(include_event_uuid)
        query['includeEventTags'] = self._make_misp_bool(include_event_tags)
        if event_timestamp is not None:
            if isinstance(event_timestamp, (list, tuple)):
                query['event_timestamp'] = (self._make_timestamp(event_timestamp[0]), self._make_timestamp(event_timestamp[1]))
            else:
                query['event_timestamp'] = self._make_timestamp(event_timestamp)
        query['sgReferenceOnly'] = self._make_misp_bool(sg_reference_only)
        query['eventinfo'] = eventinfo
        query['searchall'] = searchall
        query['requested_attributes'] = requested_attributes
        query['includeContext'] = self._make_misp_bool(include_context)
        query['headerless'] = self._make_misp_bool(headerless)
        query['includeSightings'] = self._make_misp_bool(include_sightings)
        query['includeDecayScore'] = self._make_misp_bool(include_decay_score)
        query['includeCorrelations'] = self._make_misp_bool(include_correlations)
        url = urljoin(self.root_url, f'{controller}/restSearch')
        response = self._prepare_request('POST', url, data=query)

        if return_format == 'csv':
            normalized_response_text = self._check_response(response)
            if (self.global_pythonify or pythonify) and not headerless:
                return self._csv_to_dict(normalized_response_text)  # type: ignore
            else:
                return normalized_response_text

        normalized_response = self._check_json_response(response)

        if 'errors' in normalized_response:
            return normalized_response

        if return_format == 'json' and self.global_pythonify or pythonify:
            # The response is in json, we can convert it to a list of pythonic MISP objects
            to_return: List[Union[MISPEvent, MISPAttribute]] = []
            if controller == 'events':
                for e in normalized_response:
                    me = MISPEvent()
                    me.load(e)
                    to_return.append(me)
            elif controller == 'attributes':
                # FIXME: obvs, this is hurting my soul. We need something generic.
                for a in normalized_response['Attribute']:
                    ma = MISPAttribute()
                    ma.from_dict(**a)
                    if 'Event' in ma:
                        me = MISPEvent()
                        me.from_dict(**ma.Event)
                        ma.Event = me
                    if 'RelatedAttribute' in ma:
                        related_attributes = []
                        for ra in ma.RelatedAttribute:
                            r_attribute = MISPAttribute()
                            r_attribute.from_dict(**ra)
                            if 'Event' in r_attribute:
                                me = MISPEvent()
                                me.from_dict(**r_attribute.Event)
                                r_attribute.Event = me
                            related_attributes.append(r_attribute)
                        ma.RelatedAttribute = related_attributes
                    if 'Sighting' in ma:
                        sightings = []
                        for sighting in ma.Sighting:
                            s = MISPSighting()
                            s.from_dict(**sighting)
                            sightings.append(s)
                        ma.Sighting = sightings
                    to_return.append(ma)
            elif controller == 'objects':
                raise PyMISPNotImplementedYet('Not implemented yet')
            return to_return

        return normalized_response

    def search_index(self, published: Optional[bool]=None, eventid: Optional[SearchType]=None,
                     tags: Optional[SearchParameterTypes]=None,
                     date_from: Optional[Union[datetime, date, int, str, float, None]]=None,
                     date_to: Optional[Union[datetime, date, int, str, float, None]]=None,
                     eventinfo: Optional[str]=None,
                     threatlevel: Optional[List[SearchType]]=None,
                     distribution: Optional[List[SearchType]]=None,
                     analysis: Optional[List[SearchType]]=None,
                     org: Optional[SearchParameterTypes]=None,
                     timestamp: Optional[Union[Union[datetime, date, int, str, float, None],
                                         Tuple[Union[datetime, date, int, str, float, None],
                                               Union[datetime, date, int, str, float, None]]
                                               ]]=None,
                     pythonify: Optional[bool]=None) -> Union[dict, List[MISPEvent]]:
        """Search only at the index level. Using ! in front of a value means NOT (default is OR)

        :param published: Set whether published or unpublished events should be returned. Do not set the parameter if you want both.
        :param eventid: The events that should be included / excluded from the search
        :param tags: Tags to search or to exclude. You can pass a list, or the output of `build_complex_query`
        :param date_from: Events with the date set to a date after the one specified. This filter will use the date of the event.
        :param date_to: Events with the date set to a date before the one specified. This filter will use the date of the event.
        :param eventinfo: Filter on the event's info field.
        :param threatlevel: Threat level(s) (1,2,3,4) | list
        :param distribution: Distribution level(s) (0,1,2,3) | list
        :param analysis: Analysis level(s) (0,1,2) | list
        :param org: Search by the creator organisation by supplying the organisation identifier.
        :param timestamp: Restrict the results by the timestamp (last edit). Any event with a timestamp newer than the given timestamp will be returned. In case you are dealing with /attributes as scope, the attribute's timestamp will be used for the lookup.
        :param pythonify: Returns a list of PyMISP Objects instead or the plain json output. Warning: it might use a lot of RAM
        """
        query = locals()
        query.pop('self')
        query.pop('pythonify')
        if query.get('date_from'):
            query['datefrom'] = self._make_timestamp(query.pop('date_from'))
        if query.get('date_to'):
            query['dateuntil'] = self._make_timestamp(query.pop('date_to'))

        if query.get('timestamp') is not None:
            timestamp = query.pop('timestamp')
            if isinstance(timestamp, (list, tuple)):
                query['timestamp'] = (self._make_timestamp(timestamp[0]), self._make_timestamp(timestamp[1]))
            else:
                query['timestamp'] = self._make_timestamp(timestamp)

        url = urljoin(self.root_url, 'events/index')
        response = self._prepare_request('POST', url, data=query)
        normalized_response = self._check_json_response(response)

        if not (self.global_pythonify or pythonify):
            return normalized_response
        to_return = []
        for e_meta in normalized_response:
            me = MISPEvent()
            me.from_dict(**e_meta)
            to_return.append(me)
        return to_return

    def search_sightings(self, context: Optional[str]=None,
                         context_id: Optional[SearchType]=None,
                         type_sighting: Optional[str]=None,
                         date_from: Optional[Union[datetime, date, int, str, float, None]]=None,
                         date_to: Optional[Union[datetime, date, int, str, float, None]]=None,
                         publish_timestamp: Optional[Union[Union[datetime, date, int, str, float, None],
                                                     Tuple[Union[datetime, date, int, str, float, None],
                                                           Union[datetime, date, int, str, float, None]]
                                                           ]]=None,
                         last: Optional[Union[Union[datetime, date, int, str, float, None],
                                        Tuple[Union[datetime, date, int, str, float, None],
                                              Union[datetime, date, int, str, float, None]]
                                              ]]=None,
                         org: Optional[SearchType]=None,
                         source: Optional[str]=None,
                         include_attribute: Optional[bool]=None,
                         include_event_meta: Optional[bool]=None,
                         pythonify: Optional[bool]=False
                         ) -> Union[dict, List[Dict[str, Union[MISPEvent, MISPAttribute, MISPSighting]]]]:
        '''Search sightings

        :param context: The context of the search. Can be either "attribute", "event", or nothing (will then match on events and attributes).
        :param context_id: Only relevant if context is either "attribute" or "event". Then it is the relevant ID.
        :param type_sighting: Type of sighting
        :param date_from: Events with the date set to a date after the one specified. This filter will use the date of the event.
        :param date_to: Events with the date set to a date before the one specified. This filter will use the date of the event.
        :param publish_timestamp: Restrict the results by the last publish timestamp (newer than).
        :param org: Search by the creator organisation by supplying the organisation identifier.
        :param source: Source of the sighting
        :param include_attribute: Include the attribute.
        :param include_event_meta: Include the meta information of the event.

        Deprecated:

        :param last: synonym for publish_timestamp

        :Example:

        >>> misp.search_sightings(publish_timestamp='30d') # search sightings for the last 30 days on the instance
        [ ... ]
        >>> misp.search_sightings(context='attribute', context_id=6, include_attribute=True) # return list of sighting for attribute 6 along with the attribute itself
        [ ... ]
        >>> misp.search_sightings(context='event', context_id=17, include_event_meta=True, org=2) # return list of sighting for event 17 filtered with org id 2
        '''
        query: Dict[str, Any] = {'returnFormat': 'json'}
        if context is not None:
            if context not in ['attribute', 'event']:
                raise ValueError('context has to be in {}'.format(', '.join(['attribute', 'event'])))
            url_path = f'sightings/restSearch/{context}'
        else:
            url_path = 'sightings/restSearch'
        if isinstance(context_id, (MISPEvent, MISPAttribute)):
            context_id = self.__get_uuid_or_id_from_abstract_misp(context_id)
        query['id'] = context_id
        query['type'] = type_sighting
        query['from'] = date_from
        query['to'] = date_to
        query['last'] = publish_timestamp
        query['org_id'] = org
        query['source'] = source
        query['includeAttribute'] = include_attribute
        query['includeEvent'] = include_event_meta

        url = urljoin(self.root_url, url_path)
        response = self._prepare_request('POST', url, data=query)
        normalized_response = self._check_json_response(response)
        if not (self.global_pythonify or pythonify) or 'errors' in normalized_response:
            return normalized_response

        if self.global_pythonify or pythonify:
            to_return = []
            for s in normalized_response:
                entries: Dict[str, Union[MISPEvent, MISPAttribute, MISPSighting]] = {}
                s_data = s['Sighting']
                if include_event_meta:
                    e = s_data.pop('Event')
                    me = MISPEvent()
                    me.from_dict(**e)
                    entries['event'] = me
                if include_attribute:
                    a = s_data.pop('Attribute')
                    ma = MISPAttribute()
                    ma.from_dict(**a)
                    entries['attribute'] = ma
                ms = MISPSighting()
                ms.from_dict(**s_data)
                entries['sighting'] = ms
                to_return.append(entries)
            return to_return
        return normalized_response

    def search_logs(self, limit: Optional[int]=None, page: Optional[int]=None,
                    log_id: Optional[int]=None, title: Optional[str]=None,
                    created: Optional[Union[datetime, date, int, str, float, None]]=None, model: Optional[str]=None,
                    action: Optional[str]=None, user_id: Optional[int]=None,
                    change: Optional[str]=None, email: Optional[str]=None,
                    org: Optional[str]=None, description: Optional[str]=None,
                    ip: Optional[str]=None, pythonify: Optional[bool]=False) -> Union[dict, List[MISPLog]]:
        '''Search in logs

        Note: to run substring queries simply append/prepend/encapsulate the search term with %

        :param limit: Limit the number of results returned, depending on the scope (for example 10 attributes or 10 full events).
        :param page: If a limit is set, sets the page to be returned. page 3, limit 100 will return records 201->300).
        :param log_id: Log ID
        :param title: Log Title
        :param created: Creation timestamp
        :param model: Model name that generated the log entry
        :param action: The thing that was done
        :param user_id: ID of the user doing the action
        :param change: Change that occured
        :param email: Email of the user
        :param org: Organisation of the User doing the action
        :param description: Description of the action
        :param ip: Origination IP of the User doing the action
        :param pythonify: Returns a list of PyMISP Objects instead or the plain json output. Warning: it might use a lot of RAM
        '''
        query = locals()
        query.pop('self')
        query.pop('pythonify')
        if log_id is not None:
            query['id'] = query.pop('log_id')

        response = self._prepare_request('POST', 'admin/logs/index', data=query)
        normalized_response = self._check_json_response(response)
        if not (self.global_pythonify or pythonify) or 'errors' in normalized_response:
            return normalized_response

        to_return = []
        for l in normalized_response:
            ml = MISPLog()
            ml.from_dict(**l)
            to_return.append(ml)
        return to_return

    def search_feeds(self, value: Optional[SearchParameterTypes]=None, pythonify: Optional[bool]=False) -> Union[dict, List[MISPFeed]]:
        '''Search in the feeds cached on the servers'''
        response = self._prepare_request('POST', '/feeds/searchCaches', data={'value': value})
        normalized_response = self._check_json_response(response)
        if not (self.global_pythonify or pythonify) or 'errors' in normalized_response:
            return normalized_response
        to_return = []
        for feed in normalized_response:
            f = MISPFeed()
            f.from_dict(**feed)
            to_return.append(f)
        return to_return

    # ## END Search methods ###

    # ## BEGIN Communities ###

    def communities(self, pythonify: bool=False) -> Union[dict, List[MISPCommunity]]:
        """Get all the communities."""
        r = self._prepare_request('GET', 'communities')
        communities = self._check_json_response(r)
        if not (self.global_pythonify or pythonify) or 'errors' in communities:
            return communities
        to_return = []
        for community in communities:
            c = MISPCommunity()
            c.from_dict(**community)
            to_return.append(c)
        return to_return

    def get_community(self, community: Union[MISPCommunity, int, str, UUID], pythonify: bool=False) -> Union[dict, MISPCommunity]:
        '''Get an community from a MISP instance'''
        community_id = self.__get_uuid_or_id_from_abstract_misp(community)
        r = self._prepare_request('GET', f'communities/view/{community_id}')
        community_j = self._check_json_response(r)
        if not (self.global_pythonify or pythonify) or 'errors' in community_j:
            return community_j
        c = MISPCommunity()
        c.from_dict(**community_j)
        return c

    def request_community_access(self, community: Union[MISPCommunity, int, str, UUID],
                                 requestor_email_address: str=None,
                                 requestor_gpg_key: str=None,
                                 requestor_organisation_name: str=None,
                                 requestor_organisation_uuid: str=None,
                                 requestor_organisation_description: str=None,
                                 message: str=None, sync: bool=False,
                                 anonymise_requestor_server: bool=False,
                                 mock: bool=False) -> dict:
        community_id = self.__get_uuid_or_id_from_abstract_misp(community)
        to_post = {'org_name': requestor_organisation_name,
                   'org_uuid': requestor_organisation_uuid,
                   'org_description': requestor_organisation_description,
                   'email': requestor_email_address, 'gpgkey': requestor_gpg_key,
                   'message': message, 'anonymise': anonymise_requestor_server, 'sync': sync,
                   'mock': mock}
        r = self._prepare_request('POST', f'communities/requestAccess/{community_id}', data=to_post)
        return self._check_json_response(r)

    # ## END Communities ###

    # ## BEGIN Event Delegation ###

    def event_delegations(self, pythonify: bool=False) -> Union[dict, List[MISPEventDelegation]]:
        """Get all the event delegations."""
        r = self._prepare_request('GET', 'event_delegations')
        delegations = self._check_json_response(r)
        if not (self.global_pythonify or pythonify) or 'errors' in delegations:
            return delegations
        to_return = []
        for delegation in delegations:
            d = MISPEventDelegation()
            d.from_dict(**delegation)
            to_return.append(d)
        return to_return

    def accept_event_delegation(self, delegation: Union[MISPEventDelegation, int, str], pythonify: bool=False) -> dict:
        delegation_id = self.__get_uuid_or_id_from_abstract_misp(delegation)
        r = self._prepare_request('POST', f'event_delegations/acceptDelegation/{delegation_id}')
        return self._check_json_response(r)

    def discard_event_delegation(self, delegation: Union[MISPEventDelegation, int, str], pythonify: bool=False) -> dict:
        delegation_id = self.__get_uuid_or_id_from_abstract_misp(delegation)
        r = self._prepare_request('POST', f'event_delegations/deleteDelegation/{delegation_id}')
        return self._check_json_response(r)

    def delegate_event(self, event: Optional[Union[MISPEvent, int, str, UUID]]=None,
                       organisation: Optional[Union[MISPOrganisation, int, str, UUID]]=None,
                       event_delegation: Optional[MISPEventDelegation]=None,
                       distribution: int=-1, message: str='', pythonify: bool=False) -> Union[dict, MISPEventDelegation]:
        '''Note: distribution == -1 means recipient decides'''
        if event and organisation:
            event_id = self.__get_uuid_or_id_from_abstract_misp(event)
            organisation_id = self.__get_uuid_or_id_from_abstract_misp(organisation)
            data = {'event_id': event_id, 'org_id': organisation_id, 'distribution': distribution, 'message': message}
            r = self._prepare_request('POST', f'event_delegations/delegateEvent/{event_id}', data=data)
        elif event_delegation:
            r = self._prepare_request('POST', f'event_delegations/delegateEvent/{event_id}', data=event_delegation)
        else:
            raise PyMISPError('Either event and organisation OR event_delegation are required.')
        delegation_j = self._check_json_response(r)
        if not (self.global_pythonify or pythonify) or 'errors' in delegation_j:
            return delegation_j
        d = MISPEventDelegation()
        d.from_dict(**delegation_j)
        return d

    # ## END Event Delegation ###

    # ## BEGIN Others ###

    def push_event_to_ZMQ(self, event: Union[MISPEvent, int, str, UUID]) -> dict:
        """Force push an event on ZMQ"""
        event_id = self.__get_uuid_or_id_from_abstract_misp(event)
        response = self._prepare_request('POST', f'events/pushEventToZMQ/{event_id}.json')
        return self._check_json_response(response)

    def direct_call(self, url: str, data: Optional[dict]=None, params: dict={}, kw_params: dict={}) -> Any:
        '''Very lightweight call that posts a data blob (python dictionary or json string) on the URL'''
        if data is None:
            response = self._prepare_request('GET', url, params=params, kw_params=kw_params)
        else:
            response = self._prepare_request('POST', url, data=data, params=params, kw_params=kw_params)
        return self._check_response(response, lenient_response_type=True)

    def freetext(self, event: Union[MISPEvent, int, str, UUID], string: str, adhereToWarninglists: Union[bool, str]=False,
                 distribution: Optional[int]=None, returnMetaAttributes: bool=False, pythonify: bool=False, **kwargs) -> Union[dict, List[MISPAttribute]]:
        """Pass a text to the freetext importer"""
        event_id = self.__get_uuid_or_id_from_abstract_misp(event)
        query: Dict[str, Any] = {"value": string}
        wl_params = [False, True, 'soft']
        if adhereToWarninglists in wl_params:
            query['adhereToWarninglists'] = adhereToWarninglists
        else:
            raise PyMISPError('Invalid parameter, adhereToWarninglists Can only be False, True, or soft')
        if distribution is not None:
            query['distribution'] = distribution
        if returnMetaAttributes:
            query['returnMetaAttributes'] = returnMetaAttributes
        r = self._prepare_request('POST', f'events/freeTextImport/{event_id}', data=query, **kwargs)
        attributes = self._check_json_response(r)
        if returnMetaAttributes or not (self.global_pythonify or pythonify) or 'errors' in attributes:
            return attributes
        to_return = []
        for attribute in attributes:
            a = MISPAttribute()
            a.from_dict(**attribute)
            to_return.append(a)
        return to_return

    def upload_stix(self, path, version: str='2'):
        """Upload a STIX file to MISP.
        :param path: Path to the STIX on the disk (can be a path-like object, or a pseudofile)
        :param version: Can be 1 or 2
        """
        if isinstance(path, (str, Path)):
            with open(path, 'rb') as f:
                to_post = f.read()
        else:
            to_post = path.read()

        if isinstance(to_post, bytes):
            to_post = to_post.decode()  # type: ignore

        if str(version) == '1':
            url = urljoin(self.root_url, '/events/upload_stix')
            response = self._prepare_request('POST', url, data=to_post, output_type='xml')  # type: ignore
        else:
            url = urljoin(self.root_url, '/events/upload_stix/2')
            response = self._prepare_request('POST', url, data=to_post)  # type: ignore
        return response

    # ## END Others ###

    # ## BEGIN Statistics ###

    def attributes_statistics(self, context: str='type', percentage: bool=False) -> dict:
        """Get attributes statistics from the MISP instance."""
        # FIXME: https://github.com/MISP/MISP/issues/4874
        if context not in ['type', 'category']:
            raise PyMISPError('context can only be "type" or "category"')
        if percentage:
            path = f'attributes/attributeStatistics/{context}/true'
        else:
            path = f'attributes/attributeStatistics/{context}'
        response = self._prepare_request('GET', path)
        return self._check_json_response(response)

    def tags_statistics(self, percentage: bool=False, name_sort: bool=False) -> dict:
        """Get tags statistics from the MISP instance"""
        # FIXME: https://github.com/MISP/MISP/issues/4874
        # NOTE: https://github.com/MISP/MISP/issues/4879
        if percentage:
            p = 'true'
        else:
            p = 'false'
        if name_sort:
            ns = 'true'
        else:
            ns = 'false'
        response = self._prepare_request('GET', f'tags/tagStatistics/{p}/{ns}')
        return self._check_json_response(response)

    def users_statistics(self, context: str='data') -> dict:
        """Get users statistics from the MISP instance"""
        availables_contexts = ['data', 'orgs', 'users', 'tags', 'attributehistogram', 'sightings', 'galaxyMatrix']
        if context not in availables_contexts:
            raise PyMISPError("context can only be {','.join(availables_contexts)}")
        response = self._prepare_request('GET', f'users/statistics/{context}')
        return self._check_json_response(response)

    # ## END Statistics ###

    # ## BEGIN User Settings ###

    def user_settings(self, pythonify: bool=False) -> Union[dict, List[MISPUserSetting]]:
        """Get all the user settings."""
        r = self._prepare_request('GET', 'user_settings')
        user_settings = self._check_json_response(r)
        if not (self.global_pythonify or pythonify) or 'errors' in user_settings:
            return user_settings
        to_return = []
        for user_setting in user_settings:
            u = MISPUserSetting()
            u.from_dict(**user_setting)
            to_return.append(u)
        return to_return

    def get_user_setting(self, user_setting: str, user: Optional[Union[MISPUser, int, str, UUID]]=None,
                         pythonify: bool=False) -> Union[dict, MISPUserSetting]:
        '''Get an user setting'''
        query: Dict[str, Any] = {'setting': user_setting}
        if user:
            query['user_id'] = self.__get_uuid_or_id_from_abstract_misp(user)
        response = self._prepare_request('POST', f'user_settings/getSetting')
        user_setting_j = self._check_json_response(response)
        if not (self.global_pythonify or pythonify) or 'errors' in user_setting_j:
            return user_setting_j
        u = MISPUserSetting()
        u.from_dict(**user_setting_j)
        return u

    def set_user_setting(self, user_setting: str, value: Union[str, dict], user: Optional[Union[MISPUser, int, str, UUID]]=None,
                         pythonify: bool=False) -> Union[dict, MISPUserSetting]:
        '''Get an user setting'''
        query: Dict[str, Any] = {'setting': user_setting}
        if isinstance(value, dict):
            value = json.dumps(value)
        query['value'] = value
        if user:
            query['user_id'] = self.__get_uuid_or_id_from_abstract_misp(user)
        response = self._prepare_request('POST', f'user_settings/setSetting', data=query)
        user_setting_j = self._check_json_response(response)
        if not (self.global_pythonify or pythonify) or 'errors' in user_setting_j:
            return user_setting_j
        u = MISPUserSetting()
        u.from_dict(**user_setting_j)
        return u

    def delete_user_setting(self, user_setting: str, user: Union[MISPUser, int, str, UUID]=None) -> dict:
        '''Delete a user setting'''
        query: Dict[str, Any] = {'setting': user_setting}
        if user:
            query['user_id'] = self.__get_uuid_or_id_from_abstract_misp(user)
        response = self._prepare_request('POST', f'user_settings/delete', data=query)
        return self._check_json_response(response)

    # ## END User Settings ###

    # ## BEGIN Global helpers ###

    def change_sharing_group_on_entity(self, misp_entity: Union[MISPEvent, MISPAttribute, MISPObject], sharing_group_id, pythonify: bool=False) -> Union[dict, MISPEvent, MISPObject, MISPAttribute, MISPShadowAttribute]:
        """Change the sharing group of an event, an attribute, or an object"""
        misp_entity.distribution = 4      # Needs to be 'Sharing group'
        if 'SharingGroup' in misp_entity:  # Delete former SharingGroup information
            del misp_entity.SharingGroup
        misp_entity.sharing_group_id = sharing_group_id  # Set new sharing group id
        if isinstance(misp_entity, MISPEvent):
            return self.update_event(misp_entity, pythonify=pythonify)

        if isinstance(misp_entity, MISPObject):
            return self.update_object(misp_entity, pythonify=pythonify)

        if isinstance(misp_entity, MISPAttribute):
            return self.update_attribute(misp_entity, pythonify=pythonify)

        raise PyMISPError('The misp_entity must be MISPEvent, MISPObject or MISPAttribute')

    def tag(self, misp_entity: Union[AbstractMISP, str, dict], tag: Union[MISPTag, str], local: bool=False) -> dict:
        """Tag an event or an attribute. misp_entity can be a MISPEvent, a MISP Attribute, or a UUID"""
        if isinstance(misp_entity, AbstractMISP) and 'uuid' in misp_entity:
            uuid = misp_entity.uuid
        elif isinstance(misp_entity, dict) and 'uuid' in misp_entity:
            uuid = misp_entity['uuid']
        elif isinstance(misp_entity, str):
            uuid = misp_entity
        if isinstance(tag, MISPTag):
            tag = tag.name
        to_post = {'uuid': uuid, 'tag': tag, 'local': local}
        response = self._prepare_request('POST', 'tags/attachTagToObject', data=to_post)
        return self._check_json_response(response)

    def untag(self, misp_entity: Union[AbstractMISP, str, dict], tag: Union[MISPTag, str]) -> dict:
        """Untag an event or an attribute. misp_entity can be a UUID"""
        if isinstance(misp_entity, AbstractMISP) and 'uuid' in misp_entity:
            uuid = misp_entity.uuid
        elif isinstance(misp_entity, dict) and 'uuid' in misp_entity:
            uuid = misp_entity['uuid']
        elif isinstance(misp_entity, str):
            uuid = misp_entity
        if isinstance(tag, MISPTag):
            if 'name' in tag:
                tag_name = tag.name
        else:
            tag_name = tag
        to_post = {'uuid': uuid, 'tag': tag_name}
        response = self._prepare_request('POST', 'tags/removeTagFromObject', data=to_post)
        return self._check_json_response(response)

    def build_complex_query(self, or_parameters: Optional[List[SearchType]]=None,
                            and_parameters: Optional[List[SearchType]]=None,
                            not_parameters: Optional[List[SearchType]]=None) -> dict:
        '''Build a complex search query. MISP expects a dictionary with AND, OR and NOT keys.'''
        to_return = {}
        if and_parameters:
            to_return['AND'] = and_parameters
        if not_parameters:
            to_return['NOT'] = not_parameters
        if or_parameters:
            to_return['OR'] = or_parameters
        return to_return

    # ## END Global helpers ###

    # ## Internal methods ###

    def _old_misp(self, minimal_version_required: tuple, removal_date: Union[str, date, datetime], method: Optional[str]=None, message: Optional[str]=None) -> bool:
        if self._misp_version >= minimal_version_required:
            return False
        if isinstance(removal_date, (datetime, date)):
            removal_date = removal_date.isoformat()
        to_print = f'The instance of MISP you are using is outdated. Unless you update your MISP instance, {method} will stop working after {removal_date}.'
        if message:
            to_print += f' {message}'
        warnings.warn(to_print, DeprecationWarning)
        return True

    def __get_uuid_or_id_from_abstract_misp(self, obj: Union[AbstractMISP, int, str, UUID]) -> Union[str, int]:
        if isinstance(obj, UUID):
            return str(obj)
        if isinstance(obj, (int, str)):
            return obj

        if isinstance(obj, dict) and len(obj.keys()) == 1:
            # We have an object in that format: {'Event': {'id': 2, ...}}
            # We need to get the content of that dictionary
            obj = obj[list(obj.keys())[0]]

        if isinstance(obj, MISPShadowAttribute):
            # A ShadowAttribute has the same UUID as the related Attribute, we *need* to use the ID
            return obj['id']
        if isinstance(obj, MISPEventDelegation):
            # An EventDelegation doesn't have a uuid, we *need* to use the ID
            return obj['id']
        if 'uuid' in obj:
            return obj['uuid']
        return obj['id']

    def _make_misp_bool(self, parameter: Optional[Union[bool, str]]=None) -> int:
        '''MISP wants 0 or 1 for bool, so we avoid True/False '0', '1' '''
        if parameter is None:
            return 0
        return 1 if int(parameter) else 0

    def _make_timestamp(self, value: Union[datetime, date, int, str, float, None]) -> Union[str, int, float, None]:
        '''Catch-all method to normalize anything that can be converted to a timestamp'''
        if not value:
            return None
        if isinstance(value, datetime):
            return value.timestamp()

        if isinstance(value, date):
            return datetime.combine(value, datetime.max.time()).timestamp()

        if isinstance(value, str):
            if value.isdigit():
                return value
            try:
                float(value)
                return value
            except ValueError:
                # The value can also be '1d', '10h', ...
                return value
        return value

    def _check_json_response(self, response: requests.Response) -> dict:  # type: ignore
        r = self._check_response(response, expect_json=True)
        if isinstance(r, (dict, list)):
            return r
        # Else: an exception was raised anyway

    def _check_response(self, response: requests.Response, lenient_response_type: bool=False, expect_json: bool=False) -> Union[dict, str]:
        """Check if the response from the server is not an unexpected error"""
        if response.status_code >= 500:
            logger.critical(everything_broken.format(response.request.headers, response.request.body, response.text))
            raise MISPServerError(f'Error code 500:\n{response.text}')

        if 400 <= response.status_code < 500:
            # The server returns a json message with the error details
            try:
                error_message = response.json()
            except Exception:
                raise MISPServerError(f'Error code {response.status_code}:\n{response.text}')

            logger.error(f'Something went wrong ({response.status_code}): {error_message}')
            return {'errors': (response.status_code, error_message)}

        # At this point, we had no error.

        try:
            response_json = response.json()
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(response_json)
            if isinstance(response_json, dict) and response_json.get('response') is not None:
                # Cleanup.
                response_json = response_json['response']
            return response_json
        except Exception:
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(response.text)
            if expect_json:
                raise PyMISPUnexpectedResponse(f'Unexpected response from server: {response.text}')
            if lenient_response_type and not response.headers['content-type'].startswith('application/json'):
                return response.text
            if not response.content:
                # Empty response
                logger.error('Got an empty response.')
                return {'errors': 'The response is empty.'}
            return response.text

    def __repr__(self):
        return f'<{self.__class__.__name__}(url={self.root_url})'

    def _prepare_request(self, request_type: str, url: str, data: Union[str, list, dict, AbstractMISP]={}, params: dict={},
                         kw_params: dict={}, output_type: str='json') -> requests.Response:
        '''Prepare a request for python-requests'''
        url = urljoin(self.root_url, url)
        if data == {} or isinstance(data, str):
            d = data
        elif data:
            if not isinstance(data, str):  # Else, we already have a text blob to send
                if isinstance(data, dict):  # Else, we can directly json encode.
                    # Remove None values.
                    data = {k: v for k, v in data.items() if v is not None}
                d = json.dumps(data, default=pymisp_json_default)

        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(f'{request_type} - {url}')
            if d is not None:
                logger.debug(d)

        if kw_params:
            # CakePHP params in URL
            to_append_url = '/'.join([f'{k}:{v}' for k, v in kw_params.items()])
            url = f'{url}/{to_append_url}'
        req = requests.Request(request_type, url, data=d, params=params)
        with requests.Session() as s:
            user_agent = f'PyMISP {__version__} - Python {".".join(str(x) for x in sys.version_info[:2])}'
            if self.tool:
                user_agent = f'{user_agent} - {self.tool}'
            req.auth = self.auth
            prepped = s.prepare_request(req)
            prepped.headers.update(
                {'Authorization': self.key,
                 'Accept': f'application/{output_type}',
                 'content-type': f'application/{output_type}',
                 'User-Agent': user_agent})
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(prepped.headers)
            settings = s.merge_environment_settings(req.url, proxies=self.proxies or {}, stream=None, verify=self.ssl, cert=self.cert)
            return s.send(prepped, **settings)

    def _csv_to_dict(self, csv_content: str) -> List[dict]:
        '''Makes a list of dict out of a csv file (requires headers)'''
        fieldnames, lines = csv_content.split('\n', 1)
        fields = fieldnames.split(',')
        to_return = []
        for line in csv.reader(lines.split('\n')):
            if line:
                to_return.append({fname: value for fname, value in zip(fields, line)})
        return to_return
