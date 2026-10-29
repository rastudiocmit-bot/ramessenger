// Whole application, synthetic data only. Checks controls and mobile layout.
const fs=require("node:fs"), path=require("node:path"), assert=require("node:assert/strict");
const {chromium}=require("playwright");
const web=path.join(__dirname,"../app/backend/src/center_messenger/web");
const cases=[[1440,"light"],[390,"light"],[320,"dark"]];
(async()=>{
  for(const [width,theme] of cases){
    const browser=await chromium.launch({executablePath:process.env.CM_TEST_CHROMIUM_EXECUTABLE||undefined,
      args:["--no-sandbox","--disable-dev-shm-usage",...JSON.parse(process.env.CM_TEST_CHROMIUM_ARGS||"[]")]});
    try{
      const page=await browser.newPage({viewport:{width,height:1000}}),errors=[],posts=[];
      let paused=false,retried=false;
      page.on("pageerror",error=>errors.push(error.message));
      await page.route("**/*",route=>{
        const url=new URL(route.request().url()),name=url.pathname;
        if(url.hostname!=="synthetic.example.test")return route.abort();
        if(name==="/")return route.fulfill({contentType:"text/html",body:fs.readFileSync(path.join(web,"index.html"),"utf8")});
        if(name.startsWith("/assets/")){
          const file=path.join(web,path.basename(name)),ext=path.extname(file);
          return fs.existsSync(file)?route.fulfill({contentType:({".js":"application/javascript",".css":"text/css",".woff2":"font/woff2",".jpg":"image/jpeg",".png":"image/png"})[ext],body:fs.readFileSync(file)}):route.abort();
        }
        let value;
        if(name==="/api/auth/status")value={setup_required:false};
        else if(name==="/api/auth/session")value={user:{id:"test",display_name:"Test",email:"test@example.test"},csrf:"test",
          workspaces:[{id:"test",name:"Synthetic",role:"owner",timezone:"Europe/Moscow"}]};
        else if(name.endsWith("/chats/context"))value={channels:[],branches:[],preferences:{name_order:"parent_first"}};
        else if(name.endsWith("/chats"))value={conversations:[],has_more:false,total:0};
        else if(name.endsWith("/chat-updates"))value={updates:[],next_seq:0,has_more:false};
        else if(name.endsWith("/summary"))value={incoming:0,previous_incoming:0,waiting:0,new_leads:0,average_response_seconds:null,days:[],branches:[],operators:[]};
        else if(name.endsWith("/integrations/alfa-max/sync")){
          const body=route.request().postDataJSON()||{};posts.push(body);
          if(body.archive_paused!==undefined)paused=body.archive_paused;
          if(body.branch_id===4&&body.direction==="sent")retried=true;
          value={queued:true,already_running:true,archive_paused:paused};
        }else if(name.endsWith("/sync-progress")){
          const at=new Date().toISOString();
          const streams=Array.from({length:8},(_,i)=>({branch_id:String(1+(i/2|0)),branch_name:"Филиал "+(1+(i/2|0)),direction:i%2?"sent":"receive",
            source_rows:17560,expected_rows:19588,checked_page:877,complete:![3,7].includes(i),new_messages_at:at,
            verification:{status:![3,7].includes(i)?"verified":"pending"},
            ...(i===7&&!retried?{error_code:"crm_unavailable",error:"Не удалось открыть страницу 20.",history_retry_at:at}:{})}));
          value={configured:true,state:"active",syncing:true,server_at:at,worker:{live:true,phase:paused?"reading":"projecting",
              operation:paused?"read_page":"save_message",flow:paused?"live":"archive",updated_at:at,branch_id:"2",direction:"sent",page:paused?0:877,
              client:paused?null:{name:"Клиент-пример",cards:[{customer_id:"1248",branch_id:"2",name:"Ребёнок-пример"}]},
              record_id:paused?null:"38541",position:5,position_total:20,recent:[]},
            history_loaded:false,history_complete:false,backfill_pending:!paused,archive_paused:paused,streams,
            messages:43280,stored_messages:44000,conversations:2400,last_sync_at:at,counters_as_of:at,
            last_live_cycle:{checked:8,journals:8,failed:0,added_records:7,completed_at:at},
            cards:{configured:true,total:52,ready:47,cached:50,queued:5,errors:0,as_of:at,groups:{total:117,ready:111,queued:6,errors:1,unlinked:0}},
            card_worker:{name:"Карточка-пример",student_name:"Ребёнок-пример",customer_id:"1306",branch_id:"2",manual:false,operation:"reading_customer_groups"},
            active_review:{state:"complete",processed:820,last_success_at:at,next_at:new Date(Date.now()+21600000).toISOString()}};
        }else if(name.endsWith("/integrations/alfa-max"))value={configured:true,state:"active",vault_ready:true,crm_configured:true,branches:[{id:"2",name:"Test"}]};
        else if(name.endsWith("/integrations/alfa"))value={configured:true,status:"active",vault_ready:true,branches:[{id:"2",name:"Test"}]};
        else if(name.endsWith("/integrations/max"))value={channels:[],vault_ready:true};
        else return route.fulfill({contentType:"application/json",body:"{}"});
        return route.fulfill({contentType:"application/json",body:JSON.stringify(value)});
      });
      await page.goto("https://synthetic.example.test/");
      await page.locator("#connections-tab").evaluate(el=>el.click());
      await page.locator("#alfa-max-progress").waitFor({state:"visible"});
      await page.evaluate(value=>document.documentElement.dataset.theme=value,theme);
      await page.waitForFunction(()=>document.querySelector("#alfa-loader-cards-value").textContent.includes("47 из 52"));
      const overflow=await page.locator("#alfa-max-progress").evaluate(root=>{
        const width=document.documentElement.clientWidth;
        return [...root.querySelectorAll("*")].filter(el=>el.getClientRects().length).map(el=>({text:el.textContent.slice(0,60),r:el.getBoundingClientRect()}))
          .filter(x=>x.r.left<-.5||x.r.right>width+.5).map(x=>({text:x.text,left:x.r.left,right:x.r.right}));
      });
      assert.deepEqual(overflow,[],"loader overflows "+width);
      await page.locator("#alfa-loader-history-alerts [data-retry-branch]").focus();
      await page.waitForResponse(response=>response.url().includes("/sync-progress"));
      await page.waitForFunction(()=>document.activeElement?.dataset.retryBranch==="4");
      const initial=await page.locator("#alfa-max-progress-value").textContent();
      assert.match(initial,/6 из 8/);
      await page.locator("#alfa-loader-pause").click();
      await page.waitForFunction(()=>document.querySelector("#alfa-loader-pause").textContent==="Продолжить историю");
      assert.ok(posts.some(p=>p.archive_paused===true));
      assert.equal(await page.locator("#alfa-max-progress-value").textContent(),initial);
      assert.equal(await page.locator("#alfa-loader-live-count").textContent(),"7");
      assert.equal(await page.locator("#alfa-loader-cards-badge").textContent(),"Загружается");
      await page.locator("#alfa-loader-pause").click();
      await page.waitForFunction(()=>document.querySelector("#alfa-loader-pause").textContent==="Приостановить историю");
      await page.locator("#alfa-loader-history-alerts [data-retry-branch]").click();
      await page.waitForFunction(()=>!document.querySelector("#alfa-loader-history-alerts [data-retry-branch]"));
      assert.ok(posts.some(p=>p.branch_id===4&&p.direction==="sent"));
      assert.equal(await page.locator("#alfa-max-progress-value").textContent(),initial);
      await page.locator("#alfa-max-progress-journals summary").click();
      assert.equal(await page.locator("#alfa-max-progress-streams>li").count(),8);
      await page.locator("#alfa-max-progress-journals summary").click();
      await page.locator("#alfa-max-sync").click();
      await page.waitForFunction(()=>!document.querySelector("#alfa-max-sync").disabled);
      assert.equal(await page.locator("#alfa-max-progress-value").textContent(),initial);
      const node=await page.locator("#alfa-max-progress").evaluate(el=>el.outerHTML);
      if(process.env.CM_TEST_SCREENSHOT_DIR){
        fs.mkdirSync(process.env.CM_TEST_SCREENSHOT_DIR,{recursive:true});
        await page.goto("about:blank");
        await page.setContent('<!doctype html><html lang="ru" data-theme="'+theme+'"><head><base href="https://synthetic.example.test/"><link rel="stylesheet" href="/assets/app.css"><link rel="stylesheet" href="/assets/cabinet.css"><style>html,body{height:auto!important;min-height:0!important;overflow:visible!important}body{margin:0;padding:16px;background:var(--bg)}#connections-monitor-view{display:block}</style></head><body><section id="connections-monitor-view">'+node+'</section></body></html>');
        await page.screenshot({path:path.join(process.env.CM_TEST_SCREENSHOT_DIR,"loader-"+width+"-"+theme+".png"),fullPage:true});
      }
      assert.deepEqual(errors,[]);
      console.log(JSON.stringify({width,theme,browser:browser.version(),overflow:0,controls:"passed",keyboardFocus:"retained",errors}));
    }finally{await browser.close();}
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
