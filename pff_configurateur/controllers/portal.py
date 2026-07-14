from odoo import http
from odoo.http import request
from odoo.addons.portal.controllers.portal import CustomerPortal


class PffPortalConfigurator(CustomerPortal):
    """Expose le configurateur PFF aux utilisateurs PORTAIL (distributeurs).

    Le configurateur est le même fichier statique que le back-office
    (`static/configurateur.html`), embarqué en iframe. Le dialogue iframe↔Odoo
    (protocole `postMessage`) est repris côté portail par `portal_configurator.js`
    qui appelle la route `/my/configurateur/submit` au lieu de l'ORM.
    """

    def _pff_distributor_partner(self):
        """Société du distributeur connecté. On force TOUJOURS le partenaire sur
        `commercial_partner_id` — jamais un identifiant transmis par le client —
        pour qu'un distributeur ne puisse créer un devis que pour lui-même."""
        return request.env.user.partner_id.commercial_partner_id

    @http.route(['/my/configurateur'], type='http', auth='user')
    def pff_portal_configurateur(self, **kw):
        return request.render('pff_configurateur.portal_configurateur_page', {
            'page_name': 'pff_configurateur',
        })

    @http.route(['/my/configurateur/submit'], type='jsonrpc', auth='user',
                methods=['POST'])
    def pff_portal_configurateur_submit(self, items=None, **kw):
        partner = self._pff_distributor_partner()
        # `sudo` : le distributeur portail n'a pas les droits de création sur
        # pff.configuration / sale.order. Le partenaire étant déjà forcé sur SA
        # société, il ne peut créer que ses propres documents.
        _config, order = request.env['pff.configuration'].sudo()._portal_create_from_items(
            partner, items or [])
        return {
            'ok': True,
            'order_id': order.id,
            'redirect': '/my/orders/%s' % order.id,
        }
