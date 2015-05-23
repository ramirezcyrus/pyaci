# Copyright (c) 2014, 2015 Cisco Systems, Inc. All rights reserved.

"""
pyaci.core
~~~~~~~~~~~~~~~~~~~

This module contains the core classes of PyACI.
"""

from collections import defaultdict
from lxml import etree
import json
import logging
import operator
import os
import parse
import requests

from .errors import (
    MetaError, MoError, ResourceError, RestError
)
from .utils import splitIntoRns


logger = logging.getLogger(__name__)


def subLogger(name):
    return logging.getLogger('{}.{}'.format(__name__, name))

payloadFormat = 'json'


# FIXME (2015-05-07, Praveen Kumar): We need to load the ACI meta JSON
# file. Currently, the file is specified through an environment
# variable. Find a better alternative.

# TODO (2015-05-07, Praveen Kumar): Research a way to automatically
# load this by discovering the version from the node.

aciMetaDir = os.path.expanduser(os.environ.get('ACI_META_DIR', '~/.aci-meta'))

if not os.path.exists(aciMetaDir):
    raise MetaError('Unable to find ACI meta directory {}'.format(aciMetaDir))

aciMetaFile = os.path.join(aciMetaDir, 'aci-meta.json')

if not os.path.exists(aciMetaFile):
    raise MetaError('Unable to find ACI meta file {}'.format(aciMetaFile))

with open(aciMetaFile, 'rb') as f:
    logger.debug('Loading meta information from %s', aciMetaFile)
    aciMeta = json.load(f)
    aciClassMetas = aciMeta['classes']


class Api(object):
    def __init__(self, parentApi=None):
        self._parentApi = parentApi

    def GET(self, format=None, **kwargs):
        if format is None:
            format = payloadFormat

        logger = subLogger('GET')
        rootApi = self._rootApi()
        url = self._url(format, **kwargs)
        logger.debug('-> GET %s', url)
        response = rootApi._session.request('GET', url, verify=False)
        logger.debug('<- %d', response.status_code)
        logger.debug('%s', response.text)
        if response.status_code != requests.codes.ok:
            # TODO: Parse error message and extract fields.
            raise RestError(response.text)
        return response

    def DELETE(self, format=None):
        if format is None:
            format = payloadFormat

        root = self._rootApi()
        url = self._url(format)
        logger.debug('-> DELETE %s', url)
        response = root._session.request(
            'DELETE', url, verify=False
        )
        logger.debug('<- %d', response.status_code)
        logger.debug('%s', response.text)
        if response.status_code != requests.codes.ok:
            # TODO: Parse error message and extract fields.
            raise RestError(response.text)
        return response

    def POST(self, format=None):
        if format is None:
            format = payloadFormat

        logger = subLogger('POST')
        root = self._rootApi()
        url = self._url(format)
        if format == 'json':
            data = self.Json
        elif format == 'xml':
            data = self.Xml
        logger.debug('-> POST %s', url)
        logger.debug('%s', data)
        response = root._session.request(
            'POST', url, data=data, verify=False
        )
        logger.debug('<- %d', response.status_code)
        logger.debug('%s', response.text)
        if response.status_code != requests.codes.ok:
            # TODO: Parse error message and extract fields.
            raise RestError(response.text)
        return response

    def _url(self, format=None, **kwargs):
        if format is None:
            format = payloadFormat

        def loop(entity, accumulator):
            if entity is None:
                return accumulator
            else:
                if accumulator:
                    relativeUrl = entity._relativeUrl
                    if relativeUrl:
                        passDown = entity._relativeUrl + '/' + accumulator
                    else:
                        passDown = accumulator
                else:
                    passDown = entity._relativeUrl
                return loop(entity._parentApi, passDown)

        if kwargs:
            options = '?'
            for key, value in kwargs.iteritems():
                options += (key + '=' + value + '&')
        else:
            options = ''

        return loop(self, '') + '.' + format + options

    def _rootApi(self):
        return self._parentApi._rootApi()


class Node(Api):
    def __init__(self, url, session=None):
        super(Node, self).__init__()
        self._url = url
        if session is not None:
            self._session = session
        else:
            self._session = requests.session()
        self._apiUrlComponent = 'api'

    @property
    def session(self):
        return self._session

    @property
    def mit(self):
        return Mo(self, 'topRoot')

    @property
    def method(self):
        return MethodApi(self)

    @property
    def _relativeUrl(self):
        return self._url + '/' + self._apiUrlComponent

    def _rootApi(self):
        return self

    def _testApi(self, shouldEnable, dme='policymgr'):
        if shouldEnable:
            self._apiUrlComponent = 'testapi/{}'.format(dme)
        else:
            self._apiUrlComponent = 'api'


class MoIter(Api):
    def __init__(self, parentApi, className, objects):
        self._parentApi = parentApi
        self._className = className
        self._objects = objects
        self._aciClassMeta = aciClassMetas[self._className]
        self._rnFormat = self._aciClassMeta['rnFormat']
        self._iter = self._objects.itervalues()

    def __call__(self, *args, **kwargs):
        identifiedBy = self._aciClassMeta['identifiedBy']
        if (len(args) >= 1):
            assert len(args) == len(identifiedBy)
            identifierDict = dict(zip(identifiedBy, args))
        else:
            for name in identifiedBy:
                assert name in kwargs
            identifierDict = kwargs

        rn = self._rnFormat.format(**identifierDict)
        mo = self._parentApi._getChildByRn(rn)

        if mo is None:
            if self._parentApi.TopRoot._readOnlyTree:
                raise MoError(
                    'Mo with DN {} does not contain a child with RN {}'
                    .format(self._parentApi.DN, rn))

            mo = Mo(self._parentApi, self._className)
            for name in identifiedBy:
                setattr(mo, name, identifierDict[name])
            self._parentApi._addChild(self._className, rn, mo)
            self._objects[rn] = mo

        for attribute in set(kwargs) - set(identifiedBy):
            setattr(mo, attribute, kwargs[attribute])

        return mo

    def __iter__(self):
        return self._iter

    def next(self):
        return next(self._iter)

    def __len__(self):
        return len(self._objects)


class Mo(Api):
    def __init__(self, parentApi, className):
        super(Mo, self).__init__(parentApi=parentApi)

        self._className = className
        self._aciClassMeta = aciClassMetas[self._className]
        self._properties = {
            x[0]: None
            for x in self._aciClassMeta['properties'].items()
        }
        self._rnFormat = self._aciClassMeta['rnFormat']

        self._children = {}
        self._childrenByClass = defaultdict(dict)
        self._readOnlyTree = False

    def FromDn(self, dn):
        def reductionF(acc, rn):
            dashAt = rn.find('-')
            rnPrefix = rn if dashAt == -1 else rn[:dashAt]
            className = acc._aciClassMeta['rnMap'][rnPrefix]
            return acc._spawnChildFromRn(className, rn)

        return reduce(reductionF, splitIntoRns(dn), self)

    @property
    def TopRoot(self):
        if self._isTopRoot():
            return self
        else:
            return self._parentApi.TopRoot

    @property
    def ReadOnlyTree(self):
        return self.TopRoot._readOnlyTree

    @ReadOnlyTree.setter
    def ReadOnlyTree(self, value):
        self.TopRoot._readOnlyTree = value

    @property
    def ClassName(self):
        return self._className

    @property
    def Rn(self):
        idDict = {
            k: v
            for k, v in self._properties.items()
            if k in self._aciClassMeta['identifiedBy']
        }
        return self._rnFormat.format(**idDict)

    @property
    def Dn(self):
        if self._parentApi._isTopRoot():
            return self.Rn
        else:
            return self._parentApi.Dn + '/' + self.Rn

    @property
    def Parent(self):
        if isinstance(self._parentApi, Mo):
            return self._parentApi
        else:
            return None

    def Up(self, level=1):
        result = self
        for i in range(level):
            result = result.Parent
            assert result is not None
        return result

    @property
    def Children(self):
        return self._children.itervalues()

    @property
    def Json(self):
        return json.dumps(self._dataDict(),
                          sort_keys=True, indent=2, separators=(',', ': '))

    @Json.setter
    def Json(self, value):
        self._fromObjectDict(json.loads(value))

    @property
    def Xml(self):
        def element(mo):
            result = etree.Element(mo._className)

            for key, value in mo._properties.items():
                if value is not None:
                    result.set(key, value)

            for child in mo._children.values():
                result.append(element(child))

            return result

        return etree.tostring(element(self), pretty_print=True)

    @Xml.setter
    def Xml(self, value):
        xml = bytes(bytearray(value, encoding='utf-8'))
        self._fromXmlElement(etree.fromstring(xml))

    def ParseXmlResponse(self, xml):
        # https://gist.github.com/karlcow/3258330
        xml = bytes(bytearray(xml, encoding='utf-8'))
        root = etree.fromstring(xml)
        assert root.tag == 'imdata'
        mos = []
        for element in root.iterchildren('*'):
            assert 'dn' in element.attrib
            mo = self.FromDn(element.attrib['dn'])
            mo._fromXmlElement(element)
            mos.append(mo)
        return mos

    def ParseJsonResponse(self, text):
        response = json.loads(text)
        assert 'imdata' in response
        mos = []
        for element in response['imdata']:
            name, value = element.iteritems().next()
            assert 'dn' in value['attributes']
            mo = self.FromDn(value['attributes']['dn'])
            mo._fromObjectDict(element)
            mos.append(mo)
        return mos

    def GET(self, format=None, **kwargs):
        if format is None:
            format = payloadFormat

        topRoot = self.TopRoot

        response = super(Mo, self).GET(format, **kwargs)
        if format == 'json':
            result = topRoot.ParseJsonResponse(response.text)
        elif format == 'xml':
            result = topRoot.ParseXmlResponse(response.text)

        topRoot.ReadOnlyTree = True
        return result

    @property
    def _relativeUrl(self):
        if self._className == 'topRoot':
            return 'mo'
        else:
            return self.Rn

    def _fromObjectDict(self, objectDict):
        attributes = objectDict[self._className].get('attributes', {})

        for key, value in attributes.iteritems():
            self._properties[key] = value

        children = objectDict[self._className].get('children', [])
        for cdict in children:
            className = cdict.iterkeys().next()
            attributes = cdict.itervalues().next().get('attributes', {})
            child = self._spawnChildFromAttributes(className, **attributes)
            child._fromObjectDict(cdict)

    def _fromXmlElement(self, element):
        assert element.tag == self._className

        for key, value in element.attrib.items():
            self._properties[key] = value

        for celement in element.iterchildren('*'):
            className = celement.tag
            attributes = celement.attrib
            child = self._spawnChildFromAttributes(className, **attributes)
            child._fromXmlElement(celement)

    def _dataDict(self):
        data = {}
        objectData = {}
        data[self._className] = objectData

        attributes = {
            k: v
            for k, v in self._properties.items()
            if v is not None
        }
        if attributes:
            objectData['attributes'] = attributes

        if self._children:
            objectData['children'] = []

        for child in self._children.values():
            objectData['children'].append(child._dataDict())

        return data

    def __getattr__(self, name):
        if name in self._properties:
            return self._properties[name]

        if name in self._aciClassMeta['contains']:
            return MoIter(self, name, self._childrenByClass[name])

        raise AttributeError('{} is not a valid attribute for class {}'.
                             format(name, self.ClassName))

    def __setattr__(self, name, value):
        if '_properties' in self.__dict__ and name in self._properties:
            self._properties[name] = value
        else:
            super(Mo, self).__setattr__(name, value)

    def _isTopRoot(self):
        return self._className == 'topRoot'

    def _getChildByRn(self, rn):
        return self._children.get(rn, None)

    def _addChild(self, className, rn, child):
        self._children[rn] = child
        self._childrenByClass[rn] = child

    def _spawnChildFromRn(self, className, rn):
        # TODO: Refactor.
        moIter = getattr(self, className)
        parsed = parse.parse(moIter._rnFormat, rn)
        if parsed is None:
            logging.debug('RN parsing failed, RN: {}, format: {}'.
                          format(rn, moIter._rnFormat))
            # FIXME (2015-04-08, Praveen Kumar): Hack alert!
            rn = rn.replace('[]', '[None]')
            parsed = parse.parse(moIter._rnFormat, rn)
        identifierDict = parsed.named
        orderedIdentifiers = [
            t[0] for t in sorted(parsed.spans.items(),
                                 key=operator.itemgetter(1))
        ]
        identifierArgs = [
            identifierDict[name] for name in orderedIdentifiers
        ]
        return moIter(*identifierArgs)

    def _spawnChildFromAttributes(self, className, **attributes):
        rnFormat = aciClassMetas[className]['rnFormat']
        rn = rnFormat.format(**attributes)
        return self._spawnChildFromRn(className, rn)


class LoginMethod(Api):
    def __init__(self, parentApi):
        super(LoginMethod, self).__init__(parentApi=parentApi)
        self._moClassName = 'aaaUser'
        self._properties = {}

    @property
    def Json(self):
        result = {}
        result[self._moClassName] = {'attributes': self._properties.copy()}
        return json.dumps(result,
                          sort_keys=True, indent=2, separators=(',', ': '))

    @property
    def Xml(self):
        result = etree.Element(self._moClassName)

        for key, value in self._properties.items():
            result.set(key, value)

        return etree.tostring(result, pretty_print=True)

    @property
    def _relativeUrl(self):
        return 'aaaLogin'

    def __call__(self, name, pwd):
        self._properties['name'] = name
        self._properties['pwd'] = pwd
        return self


class LoginRefreshMethod(Api):
    def __init__(self, parentApi):
        super(LoginRefreshMethod, self).__init__(parentApi=parentApi)
        self._moClassName = 'aaaRefresh'

    @property
    def Json(self):
        return ''

    @property
    def Xml(self):
        return ''

    @property
    def _relativeUrl(self):
        return 'aaaRefresh'


class UploadPackageMethod(Api):
    def __init__(self, parentApi):
        super(UploadPackageMethod, self).__init__(parentApi=parentApi)
        self._packageFile = None

    @property
    def _relativeUrl(self):
        return 'ppi/node/mo'

    def __call__(self, packageFile):
        self._packageFile = packageFile
        return self

    def POST(self, format='xml'):
        root = self._rootApi()
        assert format == 'xml'
        if not os.path.exists(self._packageFile):
            raise ResourceError('File not found: ' + self.packageFile)
        with open(self._packageFile, 'r') as f:
            response = root._session.request(
                'POST', self._url(format), files={'file': f}, verify=False
            )
        if response.status_code != requests.codes.ok:
            # TODO: Parse error message and extract fields.
            raise RestError(response.text)
        return response


class MethodApi(Api):
    def __init__(self, parentApi):
        super(MethodApi, self).__init__(parentApi=parentApi)

    @property
    def _relativeUrl(self):
        return ''

    @property
    def Login(self):
        return LoginMethod(parentApi=self)

    @property
    def LoginRefresh(self):
        return LoginRefreshMethod(parentApi=self)

    @property
    def UploadPackage(self):
        return UploadPackageMethod(parentApi=self)