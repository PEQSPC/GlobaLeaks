# -*- coding: utf-8 -*-
from OpenSSL import crypto
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from twisted.internet.defer import inlineCallbacks

from globaleaks import models
from globaleaks.handlers.base import BaseHandler
from globaleaks.models.config import ConfigFactory
from globaleaks.orm import transact, tw
from globaleaks.rest import errors, requests
from globaleaks.settings import Settings
from globaleaks.state import State
from globaleaks.utils import letsencrypt, tls
from globaleaks.utils.log import log


def db_load_tls_config(session, tid):
    """
    Transaction for loading the TLS configuration of a tenant

    :param session: An ORM session
    :param tid: A tenant ID
    :return: The serialized TLS configuration for the specified tenant
    """
    node = ConfigFactory(session, tid)

    return {
        'tid': tid,
        'ssl_key': node.get_val('https_key'),
        'ssl_cert': node.get_val('https_cert'),
        'ssl_intermediate': node.get_val('https_chain'),
        'https_enabled': node.get_val('https_enabled'),
        'hostname': node.get_val('hostname'),
    }


def db_load_tls_configs(session):
    return [db_load_tls_config(session, tid[0]) for tid in session.query(models.Tenant.id).filter(models.Tenant.active.is_(True))]


def db_generate_acme_key(session, tid):
    priv_fact = ConfigFactory(session, tid)

    key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=Settings.key_bits,
        backend=default_backend()
    )

    key = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )

    priv_fact.set_val('acme', True)
    priv_fact.set_val('acme_accnt_key', key)

    return key


def db_acme_cert_request(session, tid):
    priv_fact = ConfigFactory(session, tid)
    hostname = priv_fact.get_val('hostname')

    raw_accnt_key = priv_fact.get_val('acme_accnt_key')

    if isinstance(raw_accnt_key, str):
        raw_accnt_key = raw_accnt_key.encode()

    acme_accnt_key = serialization.load_pem_private_key(acme_accnt_key,
                                                        password=None,
                                                        backend=default_backend())

    https_key = priv_fact.get_val('https_key')

    https_cert, https_chain = letsencrypt.request_new_certificate(hostname,
                                                                  acme_accnt_key,
                                                                  https_key,
                                                                  State.tenants[tid].acme_tmp_chall_dict,
                                                                  Settings.acme_directory_url)

    priv_fact.set_val('https_cert', https_cert)
    priv_fact.set_val('https_chain', https_chain)
    State.tenants[tid].cache.https_cert = https_cert
    State.tenants[tid].cache.https_chain = https_chain


def db_load_https_key(session, tid, data=None):
    if not data:
      data = tls.gen_ecc_key()

    db_cfg = db_load_tls_config(session, tid)
    db_cfg['ssl_key'] = data

    config = ConfigFactory(session, tid)
    pkv = tls.KeyValidator()
    ok, _ = pkv.validate(db_cfg)
    if ok:
        config.set_val('https_key', data)

    return ok


def db_load_https_cert(session, tid, data):
    db_cfg = db_load_tls_config(session, tid)
    db_cfg['ssl_cert'] = data

    config = ConfigFactory(session, tid)
    pkv = tls.CertValidator()
    ok, _ = pkv.validate(db_cfg)
    if ok:
        config.set_val('https_cert', data)
        State.tenants[tid].cache.https_cert = data

    return ok


def db_load_https_chain(session, tid, data):
    db_cfg = db_load_tls_config(session, tid)
    db_cfg['ssl_intermediate'] = data

    config = ConfigFactory(session, tid)
    pkv = tls.ChainValidator()
    ok, _ = pkv.validate(db_cfg)
    if ok:
        config.set_val('https_chain', data)
        State.tenants[tid].cache.https_intermediate = data

    return ok


def db_generate_https_csr(session, tid, data):
    ConfigFactory(session, tid).set_val('https_csr', data)

    return True


def db_serialize_https_config_summary(session, tid):
    config = ConfigFactory(session, tid)

    file_summaries = {}
    for key, file_res_cls in FileHandler.mapped_resources.items():
        file_summaries[key] = file_res_cls.db_serialize(session, tid)

    return {
        'enabled': config.get_val('https_enabled'),
        'files': file_summaries,
        'acme': config.get_val('acme')
    }


def db_try_to_enable_https(session, tid):
    config = ConfigFactory(session, tid)

    cv = tls.ChainValidator()
    tls_config = db_load_tls_config(session, tid)
    tls_config['https_enabled'] = False

    ok, _ = cv.validate(tls_config)
    if not ok:
        raise errors.InputValidationError

    config.set_val('https_enabled', True)
    State.tenants[tid].cache.https_enabled = True
    State.snimap.load(tid, tls_config)


def db_disable_https(session, tid):
    config = ConfigFactory(session, tid)
    config.set_val('https_enabled', False)
    State.snimap.unload(tid)
    State.tenants[tid].cache.https_enabled = False


def db_reset_https_config(session, tid):
    config = ConfigFactory(session, tid)
    config.set_val('https_enabled', False)
    config.set_val('https_key', '')
    config.set_val('https_cert', '')
    config.set_val('https_chain', '')
    config.set_val('https_csr', '')
    config.set_val('acme', False)
    config.set_val('acme_accnt_key', '')
    State.snimap.unload(tid)
    State.tenants[tid].cache.https_enabled = False


class FileResource(object):
    """
    An interface for interacting with files stored on disk or in the db
    """
    @classmethod
    def perform_action(cls, tid):
        raise errors.MethodNotImplemented

    @staticmethod
    @transact
    def get_file(session, tid):
        raise errors.MethodNotImplemented

    @staticmethod
    @transact
    def delete_file(session, tid):
        raise errors.MethodNotImplemented

    @staticmethod
    def db_serialize(session, tid):
        raise errors.MethodNotImplemented

    @classmethod
    @transact
    def serialize(cls, session, tid):
        return cls.db_serialize(session)



class KeyFileRes(FileResource):
    @classmethod
    @inlineCallbacks
    def perform_action(cls, tid):
        yield tw(db_load_https_key, tid)

    @staticmethod
    @transact
    def delete_file(session, tid):
        config = ConfigFactory(session, tid)
        config.set_val('https_key', '')

    @staticmethod
    def db_serialize(session, tid):
        config = ConfigFactory(session, tid)

        return {
            'set': config.get_val('https_key') != ''
        }


class CertFileRes(FileResource):
    @staticmethod
    @transact
    def delete_file(session, tid):
        ConfigFactory(session, tid).set_val('https_cert', '')
        State.tenants[tid].cache.https_cert = ''

    @staticmethod
    def db_serialize(session, tid):
        c = ConfigFactory(session, tid).get_val('https_cert')
        if not c:
            return {
                'name': 'cert',
                'set': False
            }

        c = """{}""".format(c)
        x509 = crypto.load_certificate(crypto.FILETYPE_PEM, c)
        expr_date = letsencrypt.convert_asn1_date(x509.get_notAfter())

        return {
            'name': 'cert',
            'issuer': tls.parse_issuer_name(x509),
            'expiration_date': expr_date,
            'set': True,
        }


class ChainFileRes(FileResource):
    @staticmethod
    @transact
    def delete_file(session, tid):
        ConfigFactory(session, tid).set_val('https_chain', '')

    @staticmethod
    def db_serialize(session, tid):
        c = ConfigFactory(session, tid).get_val('https_chain')
        if not c:
            return {
                'name': 'chain',
                'set': False
            }

        c = tls.split_pem_chain(c)[0]
        x509 = crypto.load_certificate(crypto.FILETYPE_PEM, c)
        expr_date = letsencrypt.convert_asn1_date(x509.get_notAfter())

        return {
            'name': 'chain',
            'issuer': tls.parse_issuer_name(x509),
            'expiration_date': expr_date,
            'set': True
        }


class CsrFileRes(FileResource):
    @staticmethod
    @transact
    def delete_file(session, tid):
        ConfigFactory(session, tid).set_val('https_csr', '')

    @staticmethod
    @transact
    def get_file(session, tid):
        return ConfigFactory(session, tid).get_val('https_csr')

    @staticmethod
    def db_serialize(session, tid):
        csr = ConfigFactory(session, tid).get_val('https_csr')
        return {
            'name': 'csr',
            'set': len(csr) != 0
        }


class FileHandler(BaseHandler):
    check_roles = 'admin'
    root_tenant_or_management_only = True

    mapped_resources = {
        'key': KeyFileRes,
        'cert': CertFileRes,
        'chain': ChainFileRes,
        'csr': CsrFileRes
    }

    def get_res_or_raise(self, name):
        if name not in self.mapped_resources:
            raise errors.MethodNotImplemented

        return self.mapped_resources[name]

    def delete(self, name):
        return self.get_res_or_raise(name).delete_file(self.request.tid)

    @inlineCallbacks
    def post(self, name):
        req = self.validate_request(self.request.content.read(),
                                    requests.AdminTLSCfgFileResourceDesc)

        if name == 'key':
            ok = yield tw(db_load_https_key, self.request.tid, req['content'])
        elif name == 'cert':
            ok = yield tw(db_load_https_cert, self.request.tid, req['content'])
        elif name == 'chain':
            ok = yield tw(db_load_https_chain, self.request.tid, req['content'])
        else:
            ok = False

        if not ok:
            raise errors.InputValidationError

    @inlineCallbacks
    def put(self, name):
        file_res_cls = self.get_res_or_raise(name)

        yield file_res_cls.perform_action(self.request.tid)

    def get(self, name):
        return self.get_res_or_raise(name).get_file(self.request.tid)


class ConfigHandler(BaseHandler):
    check_roles = 'admin'
    root_tenant_or_management_only = True

    def get(self):
        return tw(db_serialize_https_config_summary, self.request.tid)

    def post(self):
        tw(db_try_to_enable_https, self.request.tid)

    def put(self):
        tw(db_disable_https, self.request.tid)

    def delete(self):
        tw(db_reset_https_config, self.request.tid)


class CSRFileHandler(FileHandler):
    check_roles = 'admin'
    root_tenant_or_management_only = True

    @inlineCallbacks
    def post(self, name):
        request = self.validate_request(self.request.content.read(),
                                        requests.AdminCSRFileDesc)

        desc = request['content']
        csr_fields = {
            'C': desc['country'].upper(),
            'ST': desc['province'],
            'L': desc['city'],
            'O': desc['company'],
            'OU': desc['company'],
            'CN': State.tenants[self.request.tid].cache.hostname,
            'emailAddress': desc['email'],
        }

        csr_txt = yield self.perform_action(self.request.tid, csr_fields)

        ok = yield tw(db_generate_https_csr, self.request.tid, csr_txt)
        if not ok:
            raise errors.InputValidationError

    @staticmethod
    @transact
    def perform_action(session, tid, csr_fields):
        db_cfg = db_load_tls_config(session, tid)

        pkv = tls.KeyValidator()
        ok, _ = pkv.validate(db_cfg)
        if not ok:
            raise errors.InputValidationError

        key_pair = db_cfg['ssl_key']
        return tls.gen_x509_csr_pem(key_pair, csr_fields, Settings.csr_sign_bits)


class AcmeHandler(BaseHandler):
    check_roles = 'admin'
    root_tenant_or_management_only = True

    def post(self):
        return tw(db_acme_cert_request, self.request.tid)


class AcmeChallengeHandler(BaseHandler):
    check_roles = 'any'

    def get(self, token):
        tmp_chall_dict = State.tenants[self.request.tid].acme_tmp_chall_dict
        if token in tmp_chall_dict:
            return tmp_chall_dict[token].tok

        raise errors.ResourceNotFound
