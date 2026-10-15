"""Source contract suggestions for agent-owned storage binding; never accepted datasets."""
from dotnet_syntax import close, split, values
from dotnet_boundaries import literal


def discover(reader):
    candidates = []
    for path, source in reader.sources.items():
        tokens = source['tokens']
        for i, token in enumerate(tokens):
            if token.kind != 'code': continue
            if token.value in {'Serialize', 'SerializeToUtf8Bytes', 'Deserialize'} and values(tokens[max(0,i-8):i]) == 'System . Text . Json . JsonSerializer .':
                if values(tokens[i+1:i+2]) != '<': continue
                try:
                    end = close(tokens,i+1,'<','>')
                    if values(tokens[end+1:end+2]) != '(': continue
                    last = close(tokens,end+1,'(',')')
                    arguments = split(tokens[end+2:last])
                except ValueError: continue
                if len(arguments) != 1:
                    candidates.append({'kind':'serialized_contract','refs':[f'{path}:{token.line}'],'binding_status':'agent_investigation_required','limitation':'Serializer options or context require source investigation.'})
                    continue
                if any(model['qualified']=='System.Text.Json.JsonSerializer' for model in reader.models): continue
                name = ''.join(t.value for t in tokens[i+2:end])
                owner = {'path':path,'line':token.line,'project':source['project'],'namespace':''}
                model = reader.resolve(name, owner, required=False)
                if not model: continue
                before = len(reader.gaps)
                fields = reader.fields(model, 'json')
                limitations = reader.gaps[before:]
                del reader.gaps[before:]
                candidates.append({'kind':'serialized_contract','type':model['qualified'],'operation':token.value,
                    'refs':[f'{path}:{token.line}',f"{model['path']}:{model['line']}"], 'fields':fields,
                    'binding_status':'agent_investigation_required', 'limitations':limitations,
                    'required_investigation':'Trace the serialized value to its Redis write, message publication or other destination; establish logical store identity and activation before adding a supplemental dataset.'})
            for api, method in [('SecureStorage','SetAsync'),('Preferences','Set')]:
                prefix = f'Microsoft . Maui . Storage . {api} . Default .'
                if token.value != method or values(tokens[max(0,i-10):i]) != prefix: continue
                if any(model['qualified']==f'Microsoft.Maui.Storage.{api}' for model in reader.models): continue
                if values(tokens[i+1:i+2]) != '(': continue
                try: args = split(tokens[i+2:close(tokens,i+1,'(',')')])
                except ValueError: continue
                if len(args)!=2 or literal(args[0]) is None:
                    candidates.append({'kind':'device_storage','refs':[f'{path}:{token.line}'],'binding_status':'agent_investigation_required','limitation':'Dynamic key or container requires source investigation.'})
                    continue
                # SecureStorage's documented value contract is string; Preferences is overloaded.
                shape = 'string' if api=='SecureStorage' or literal(args[1]) is not None else None
                if shape is None:
                    candidates.append({'kind':'device_storage','refs':[f'{path}:{token.line}'],'binding_status':'agent_investigation_required','limitation':'Preferences overload requires value-type evidence.'})
                    continue
                candidates.append({'kind':'device_storage','api':api,'key':literal(args[0]),'fields':[{'name':'value','shape':shape,'evidence':[reader.span({'path':path},token.line)]}],
                    'refs':[f'{path}:{token.line}'],'binding_status':'agent_investigation_required',
                    'required_investigation':'Verify framework identity, activation and device/container scope. Do not merge keys across applications or infer personal meaning.'})
    return candidates
