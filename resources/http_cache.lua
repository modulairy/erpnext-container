-- Cache only public representations. Redis errors are cache misses, never site outages.
local M = {}
local config

function M.init(path)
    local file = assert(io.open(path, 'r'))
    config = assert(require('cjson.safe').decode(file:read('*a')))
    file:close()
end

function M.prepare()
    if not config.active then return '1' end
    local site = ngx.var.frappe_cache_site
    if not config.sites[site] then return '1' end
    if ngx.req.get_method() ~= 'GET' or ngx.var.http_range then return '1' end
    if ngx.var.http_cache_kind ~= 'static' then
        if ngx.var.http_authorization or ngx.var.http_cookie then return '1' end
        local uri = ngx.var.uri
        if uri:match('^/api[/]?') or uri:match('^/app[/]?') or uri:match('^/private/')
            or uri:match('^/protected/') or uri:match('^/socket.io') or uri:match('^/login')
            or uri:match('^/printview') or uri:match('^/backups/') then return '1' end
    end
    ngx.var.http_cache_site = site
    -- Host, scheme, language and original query are representation boundaries.
    ngx.var.http_cache_key = config.prefix..':'..site..':'..ngx.var.http_cache_kind..':'..ngx.md5(
        ngx.var.scheme..'|'..ngx.var.host..'|'..ngx.var.request_uri..'|'..(ngx.var.http_accept_language or ''))
    ngx.var.http_cache_query = 'site='..ngx.escape_uri(site)..'&key='..ngx.escape_uri(ngx.var.http_cache_key)..'&kind='..ngx.var.http_cache_kind
    return '0'
end

local function connection()
    local settings = config.sites[ngx.unescape_uri(ngx.var.arg_site or '')]
    if not settings then return nil end
    local red = require('resty.redis'):new()
    red:set_timeout(config.timeout)
    local ok = red:connect(settings.host, settings.port, {
        ssl=settings.tls, ssl_verify=true, server_name=settings.host,
        pool=config.prefix..':'..ngx.var.arg_site, pool_size=config.pool_size,
        backlog=config.pool_size * 2})
    if not ok then red:close(); return nil end
    if red:get_reused_times() == 0 then
        if settings.password ~= '' then
            if settings.username ~= '' then ok = red:auth(settings.username, settings.password)
            else ok = red:auth(settings.password) end
            if not ok then red:close(); return nil end
        end
        ok = red:select(settings.database)
        if not ok then red:close(); return nil end
    end
    return red
end

function M.fetch()
    local red = connection()
    if not red then return ngx.exit(404) end
    local body = red:get(ngx.unescape_uri(ngx.var.arg_key or ''))
    red:set_keepalive(10000, config.pool_size)
    if not body or body == ngx.null then return ngx.exit(404) end
    ngx.print(body)
end

function M.headers()
    if ngx.is_subrequest then return end
    local hit = ngx.var.srcache_fetch_status == 'HIT'
    ngx.header['X-HTTP-Redis-Cache'] = ngx.var.http_cache_skip == '1' and 'BYPASS' or (hit and 'HIT' or 'MISS')
    if hit or ngx.var.http_cache_skip == '1' or ngx.status ~= 200 then return end
    local control = ngx.header['Cache-Control'] or ''
    if type(control) == 'table' then control = table.concat(control, ',') end
    control = control:lower()
    if ngx.header['Set-Cookie'] or control:find('private', 1, true)
        or control:find('no-store', 1, true) or control:find('no-cache', 1, true) then return end
    local vary = ngx.header['Vary'] or ''
    if type(vary) == 'table' then vary = table.concat(vary, ',') end
    for field in vary:lower():gmatch('[^,%s]+') do
        if field ~= 'accept-encoding' and field ~= 'accept-language' then return end
    end
    if ngx.header['Content-Encoding'] then return end
    local length = tonumber(ngx.header['Content-Length'])
    if length and length > config.max_size then return end
    if ngx.var.http_cache_kind == 'static' then
        ngx.header['Cache-Control'] = 'public, max-age='..config.static_ttl
    else
        local age = tonumber(control:match('s%-maxage=(%d+)') or control:match('max%-age=(%d+)'))
        if not control:find('public', 1, true) or not age or age <= 0 then return end
    end
end

function M.store()
    ngx.req.read_body()
    local body = ngx.req.get_body_data()
    if not body then
        local name = ngx.req.get_body_file()
        local file = name and io.open(name, 'rb')
        if file then body = file:read(config.max_size+1); file:close() end
    end
    if not body or #body > config.max_size then return ngx.exit(204) end
    local red = connection()
    if not red then return ngx.exit(204) end
    local maximum = ngx.var.arg_kind == 'static' and config.static_ttl or config.public_ttl
    local ttl = math.min(tonumber(ngx.var.arg_ttl) or maximum, maximum)
    if ttl > 0 then red:set(ngx.unescape_uri(ngx.var.arg_key or ''), body, 'EX', ttl) end
    red:set_keepalive(10000, config.pool_size)
    return ngx.exit(204)
end

return M
