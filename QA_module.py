import torch
import numpy
import torch.nn.functional as F
import ujson as json
import torch.optim as optim
import numpy as np
import time
import datetime
import random
from transformers import BertTokenizer, BertModel, AutoTokenizer,AutoModelForQuestionAnswering, BertForQuestionAnswering, BertConfig, BertForSequenceClassification, get_linear_schedule_with_warmup
from collections import Counter
from util import batch, format_time
from hotpot_evaluate_v1 import f1_score, exact_match_score

def preprocess_single_qapair(single_hotpot_qapair, model, tokenizer, answer):
    MAX_LEN = 512
    question = single_hotpot_qapair['question']
    paragraphs = single_hotpot_qapair['context']
    line_before_para_tokens = tokenizer.convert_tokens_to_ids(tokenizer.tokenize("[CLS] " + question + " [SEP] "))
    line_after_para_tokens = tokenizer.convert_tokens_to_ids(tokenizer.tokenize(" [SEP] " + answer + " [SEP]"))
    len_without_para_tokens = len(line_before_para_tokens) + len(line_after_para_tokens)
    
    if len_without_para_tokens > MAX_LEN:
       SEP_id = line_after_para_tokens[-1]
       line_len = len(line_after_para_tokens)
       line_after_para_tokens = line_after_para_tokens[:min(line_len//2, 10)]
       line_after_para_tokens.append(SEP_id)
       len_without_para_tokens =  len(line_before_para_tokens) + len(line_after_para_tokens)
    
    segment_before_para = [0 for _ in range(len(line_before_para_tokens))]
    segment_after_para = [0 for _ in range(len(line_after_para_tokens))]

    sentences = []
    
    for idx, para in enumerate(paragraphs):
        para_tokens = []
        for sentence in para[1]:
            sentence_tokens = tokenizer.convert_tokens_to_ids(tokenizer.tokenize(sentence))
            para_tokens += sentence_tokens
        
        allowed_para_length = len(para_tokens)
        if len(para_tokens)+len_without_para_tokens>MAX_LEN:
            para_tokens = para_tokens[0:MAX_LEN-len_without_para_tokens]
            allowed_para_length = len(para_tokens)

        indexed_tokens = line_before_para_tokens + para_tokens + line_after_para_tokens
        attention_mask = [1 for _ in range(len(indexed_tokens))] + [0 for _ in range(MAX_LEN-len(indexed_tokens))]

        # Pad the tokens to MAX_LEN
        indexed_tokens += [0 for _ in range(MAX_LEN-len(indexed_tokens))]
        pos = 0

        for sidx, sentence in enumerate(para[1]):
            sentence_token = tokenizer.convert_tokens_to_ids(tokenizer.tokenize(sentence))
            segment_para = [0 for _ in range(allowed_para_length)]
            if pos+len(sentence_token) > allowed_para_length:
                if pos < allowed_para_length:
                    segment_para[pos:] = [1 for _ in range(allowed_para_length - pos)]
            else:
                segment_para[pos:pos+len(sentence_token)] = [1 for _ in range(len(sentence_token))]
                pos = pos + len(sentence_token)

            sentence_segment_id = segment_before_para + segment_para + segment_after_para
            sentence_segment_id += [0 for _ in range(MAX_LEN-len(sentence_segment_id))]

            single_sentence_info ={}
            single_sentence_info['score'] = 0
            single_sentence_info['para'] = idx
            single_sentence_info['sentence'] = sentence
            single_sentence_info['sentence_idx_in_para'] = sidx
            single_sentence_info['title'] = para[0]
            single_sentence_info['first_sentence'] = para[1][0]
            single_sentence_info['indexed_tokens'] = indexed_tokens #?????? ????????? ??? ???
            single_sentence_info['attention_mask'] = attention_mask
            single_sentence_info['segment_id'] = sentence_segment_id
    
            sentences.append(single_sentence_info)

    pos = 0
    for single_batch in batch(sentences, 8):
        
        inputs_ids = []
        attention_masks = []
        segment_ids = []

        for single_sentence_info in single_batch:
            inputs_ids.append(single_sentence_info['indexed_tokens'])
            attention_masks.append(single_sentence_info['attention_mask'])
            segment_ids.append(single_sentence_info['segment_id'])

        b_inputs_ids = torch.Tensor(inputs_ids).cuda().long()
        b_segment_ids = torch.Tensor(segment_ids).cuda().long()
        b_attention_masks = torch.Tensor(attention_masks).cuda().long()
        with torch.no_grad():
            logits = model(input_ids = b_inputs_ids, token_type_ids=b_segment_ids, attention_mask=b_attention_masks)
            logits_list = list(logits[0])
        for logit in logits_list:
            sentences[pos]['score'] = logit[1].item()
            pos=pos+1
    score_sorted_sentences = sorted(sentences, key=(lambda x : x['score']), reverse=True)
    return score_sorted_sentences


def prepare_file_for_qa(original_hotpotqa_file, data_category, rnas_model, ss_tokenizer, qa_tokenizer):
    print("Loading {} ...".format(original_hotpotqa_file))
    data = json.load(open(original_hotpotqa_file, 'r'))
    print("Successfully loaded the data!")
    Num_total_datas = len(data)
    start_time = time.time()
    prepared_datas = []
    for myidx, qapair in enumerate(data):
        para_highest_score = {}
        for para_idx in range(len(qapair['context'])):
            para_highest_score[para_idx] = -99999

        single_qa_line={}
        sorted_sentences = preprocess_single_qapair(qapair, rnas_model, ss_tokenizer, "[MASK]")
        line_before_E_tokens = qa_tokenizer.convert_tokens_to_ids(qa_tokenizer.tokenize("[CLS] " + qapair['question'] + " [SEP] "))
        line_after_E_tokens = qa_tokenizer.convert_tokens_to_ids(qa_tokenizer.tokenize(" yes no noans [SEP]"))
        
        E_tokens=[]        

        # pick two paragraph with highest score
        for single_sentence_info in sorted_sentences:
            sentence_para_idx = single_sentence_info['para']
            sentence_score = single_sentence_info['score']
            if para_highest_score[sentence_para_idx] < sentence_score:
                para_highest_score[sentence_para_idx] = sentence_score
        
        para_highest_score_sorted = sorted(para_highest_score.items(), key=(lambda x: x[1]), reverse=True)
        selected_para_idx = [para_highest_score_sorted[0][0], para_highest_score_sorted[1][0]]

       
        for para_idx in selected_para_idx:
            para_title = qapair['context'][para_idx][0]
            para_sentences = qapair['context'][para_idx][1]
            E_tokens += qa_tokenizer.convert_tokens_to_ids(qa_tokenizer.tokenize("<t>" +para_title + "</t>"))
            for sentence in para_sentences:
                E_tokens += qa_tokenizer.convert_tokens_to_ids(qa_tokenizer.tokenize(sentence))
            

            if len(line_before_E_tokens + E_tokens + line_after_E_tokens) > 512:
                # truncate E_tokens to fit in 512
                E_tokens = E_tokens[:512-len(line_before_E_tokens + line_after_E_tokens)]
                break
        
        
            
        qa_line = line_before_E_tokens + E_tokens + line_after_E_tokens

        
        tokenized_answer = qa_tokenizer.convert_tokens_to_ids(qa_tokenizer.tokenize(qapair['answer']))
        answer_len = len(tokenized_answer)
        start_position = 0
        end_position = 0

        for idx in range(len(line_before_E_tokens), len(qa_line)-answer_len+1):
            candidate_span = qa_line[idx:idx+answer_len]
            if candidate_span == tokenized_answer:
                start_position = idx
                end_position = idx + answer_len - 1
                break
        
        # If we cannot find answer within the context, mark the answer as noanswer.
        if start_position == 0 and end_position == 0:
            noans_token_len = len(qa_tokenizer.convert_tokens_to_ids(qa_tokenizer.tokenize("noans")))
            end_position = len(qa_line)-2
            start_position = end_position - noans_token_len +1

        segment_id = [0 for _ in range(len(line_before_E_tokens))] + [1 for _ in range(len(E_tokens + line_after_E_tokens))]
        attention_mask = [1 for _ in range(len(qa_line))]

        # pad the tokens 
        qa_line = qa_line + [0 for _ in range(512-len(qa_line))]
        segment_id = segment_id + [0 for _ in range(512-len(segment_id))]
        attention_mask = attention_mask + [0 for _ in range(512-len(attention_mask))]

        single_qa_line['line'] = qa_line
        single_qa_line['segment_id'] = segment_id
        single_qa_line['attention_mask'] = attention_mask
        single_qa_line['start_position'] = start_position
        single_qa_line['end_position'] = end_position
        prepared_datas.append(single_qa_line)

        if myidx%50 == 0:
            time_taken = time.time()-start_time
            print("Done [{}/{}], elapsed = {}".format(myidx, Num_total_datas, format_time(time_taken)))
    print("Saving {}_data_for_qa".format(data_category))
    with open(data_category+"_data_for_qa.json", "w") as fh:
        json.dump(prepared_datas, fh)

def find_best_answer(start_scores, end_scores):
    
    start_tokens_idxs = torch.argmax(start_scores, dim=1).tolist()
    end_tokens_idxs = torch.argmax(end_scores, dim=1).tolist()
    

    start_scores_l = start_scores.tolist()
    end_scores_l = end_scores.tolist()

    valid_start_token_idxs = []
    valid_end_tokens_idxs = []

    for idx in range(len(start_tokens_idxs)):
        if start_tokens_idxs[idx]<=end_tokens_idxs[idx]:
            valid_start_token_idxs.append(start_tokens_idxs[idx])
            valid_end_tokens_idxs.append(end_tokens_idxs[idx])
        else:
            cur_start_token_score = start_scores_l[idx]
            cur_end_token_score = end_scores_l[idx]

            max_score = -100000000
            max_start_idx = 0
            max_end_idx = 0

            search_area_len = len(cur_start_token_score)

            for i in range(search_area_len):
                for j in range(i, search_area_len):
                    tmp = cur_start_token_score[i] + cur_end_token_score[j]
                    if max_score > tmp:
                        max_score = tmp
                        max_start_idx = i
                        max_end_idx = j
                    
            valid_start_token_idxs.append(max_start_idx)
            valid_end_tokens_idxs.append(max_end_idx)
    
    return valid_start_token_idxs, valid_end_tokens_idxs


def train_and_evaluate_QA_module():

    
    print("Loading tokenizer for sentence scoring module..")
    ss_tokenizer = BertTokenizer.from_pretrained('bert-base-cased')
    print("Loading tokenizer for question answering module..")
    qa_tokenizer = BertTokenizer.from_pretrained('bert-base-cased')

    print("Loading sentence scorer model..")
    rnas_model = BertForSequenceClassification.from_pretrained("./model/rnas/")
    rnas_model.cuda()
    rnas_model.eval()

    '''

    print("Preparing training data..")
    prepare_file_for_qa("hotpot_train_v1.1.json", "Training", rnas_model, ss_tokenizer, qa_tokenizer)
    print("Preparing dev data..")
    prepare_file_for_qa("hotpot_dev_distractor_v1.json", "Dev", rnas_model, ss_tokenizer, qa_tokenizer)
    '''

    '''
    print("Loading training datasets..")
    training_data = json.load(open("Training_data_for_qa.json", 'r'))
    
    randomlist = random.sample(range(0,90447), k = 7500*5)

    tmp_training_data = []

    for i in randomlist:
        tmp_training_data.append(training_data[i])

    file_path1 = "./short_Training_data_for_qa"

    with open(file_path1, 'w') as outfile:
        json.dump(tmp_training_data, outfile)
    '''
    rnas_model.cpu()

    print("Loading training datasets..")
    train_dataset = json.load(open("Training_data_for_qa.json", 'r'))

    print("Loading dev datasets..")
    dev_dataset = json.load(open("Dev_data_for_qa.json"))
    
    print("Loading QA model..")	
    QA_model = BertForQuestionAnswering.from_pretrained('bert-base-cased')
    # QA_model.resize_token_embeddings(len(qa_tokenizer))
    QA_model.cuda()

    batch_size = 4
    num_epochs = 5
    optimizer = optim.Adam(QA_model.parameters(), lr=1e-5, weight_decay=0.01)
    total_training_steps = len(train_dataset) // batch_size if len(train_dataset) % batch_size ==0 else (len(train_dataset) // batch_size)+1
    scheduler = get_linear_schedule_with_warmup(optimizer, num_warmup_steps= total_training_steps//10, num_training_steps=total_training_steps)

    # ============
    #   Training
    # ============

    print("Now training...")
    training_stats = []

    for epoch in range(num_epochs):
        training_epoch_start_time = time.time()
        print("Shuffling dataset...")
        random.shuffle(train_dataset)
        print("")
        print('======== Epoch {:} / {:} ========'.format(epoch + 1, num_epochs))
        print('Training...')
        total_train_loss= 0
        QA_model.train()
        step = 0
        for single_batch in batch(train_dataset, batch_size):
            inputs_ids =[]
            attention_masks =[]
            segment_ids =[]
            start_positions =[]
            end_positions = []
            for single_qa_line in single_batch:
                inputs_ids.append(single_qa_line['line']) 
                segment_ids.append(single_qa_line['segment_id']) 
                attention_masks.append(single_qa_line['attention_mask'])
                start_positions.append(single_qa_line['start_position'])
                end_positions.append(single_qa_line['end_position'])
            
            b_inputs_ids = torch.Tensor(inputs_ids).cuda().long()
            b_segment_ids = torch.Tensor(segment_ids).cuda().long()
            b_attention_masks = torch.Tensor(attention_masks).cuda().long()
            b_start_positions = torch.Tensor(start_positions).cuda().long()
            b_end_positions = torch.Tensor(end_positions).cuda().long()

            QA_model.zero_grad()
            outputs = QA_model(input_ids = b_inputs_ids, attention_mask=b_attention_masks, token_type_ids=b_segment_ids, start_positions = b_start_positions, end_positions = b_end_positions)
            #loss, start_scores, end_scores
            loss = outputs.loss
            start_scores = outputs.start_logits
            end_scores = outputs.end_logits

            total_train_loss += loss.item()
            
            loss.backward()
            optimizer.step()
            scheduler.step()

            step+=1
            if step % 100 == 0 and step != 0:
                elapsed_epoch_time = time.time()-training_epoch_start_time
                print("Batch [ {} / {} ] , loss = {} , elapsed = {}".format(step, total_training_steps, loss.item(), format_time(elapsed_epoch_time)))
        avg_train_loss = total_train_loss / step
        Training_time = format_time(time.time()-training_epoch_start_time)
        print("Epoch {} average training loss : {}".format(epoch+1, avg_train_loss))
        print("Epoch {} took : ".format(Training_time))

        # ==============
        #   Validation
        # ==============

        print("Now validating...")
        QA_model.eval()
        validation_epoch_start_time = time.time()

        total_f1_score = 0
        total_EM_score = 0
        total_eval_loss = 0
        

        for single_batch in batch(dev_dataset, batch_size):
            inputs_ids =[]
            attention_masks =[]
            segment_ids =[]
            start_positions =[]
            end_positions = []
            ground_truths = []
            for single_qa_line in single_batch:
                token_line = single_qa_line['line']
                start_position = single_qa_line['start_position']
                end_position = single_qa_line['end_position']

                inputs_ids.append(token_line)
                segment_ids.append(single_qa_line['segment_id']) 
                attention_masks.append(single_qa_line['attention_mask'])
                start_positions.append(start_position)
                end_positions.append(end_position)

                answer_part = token_line[start_position:end_position+1]
                answer = ' '.join(qa_tokenizer.convert_ids_to_tokens(answer_part))

                corrected_answer = ''
                for word in answer.split():
                    #If it's a subword token
                    if word[0:2] == '##':
                        corrected_answer += word[2:]
                    else:
                        corrected_answer += ' ' + word

                ground_truths.append(corrected_answer)

            b_inputs_ids = torch.Tensor(inputs_ids).cuda().long()
            b_segment_ids = torch.Tensor(segment_ids).cuda().long()
            b_attention_masks = torch.Tensor(attention_masks).cuda().long()
            b_start_positions = torch.Tensor(start_positions).cuda().long()
            b_end_positions = torch.Tensor(end_positions).cuda().long()

            with torch.no_grad():
                outputs = QA_model(input_ids = b_inputs_ids, attention_mask=b_attention_masks, token_type_ids=b_segment_ids, start_positions = b_start_positions, end_positions = b_end_positions)
                loss = outputs.loss
                start_scores = outputs.start_logits
                end_scores = outputs.end_logits


            total_eval_loss += loss.item()

            start_tokens_idxs, end_tokens_idxs = find_best_answer(start_scores, end_scores)
            predicted_answers = []
            for i in range(len(start_tokens_idxs)):
                token_line = inputs_ids[i]
                answer_part = token_line[start_tokens_idxs[i]:end_tokens_idxs[i]+1]
                answer = ' '.join(qa_tokenizer.convert_ids_to_tokens(answer_part))

                corrected_answer = ''
                for word in answer.split():
                    #If it's a subword token
                    if word[0:2] == '##':
                        corrected_answer += word[2:]
                    else:
                        corrected_answer += ' ' + word
                predicted_answers.append(corrected_answer)
            
            for i in range(len(start_tokens_idxs)):
                
                single_f1_score, _, _ = f1_score(predicted_answers[i], ground_truths[i])
                single_EM_score = exact_match_score(predicted_answers[i], ground_truths[i])

                total_f1_score += single_f1_score
                total_EM_score += single_EM_score

        avg_eval_loss = total_eval_loss / len(dev_dataset)
        avg_EM_score = 100.0 * total_EM_score / len(dev_dataset)
        avg_f1_score = 100.0 * total_f1_score / len(dev_dataset)
        Validation_time = time.time()-validation_epoch_start_time

        print("Epoch {} average validation loss : {}".format(epoch+1, avg_eval_loss))
        print("Epoch {} average validation f1 score : {}".format(epoch+1, avg_f1_score))
        print("Epoch {} average validation EM score : {}".format(epoch+1, avg_EM_score))

        training_stats.append(
            {
                'epoch': epoch+1,
                'Training_Loss': avg_train_loss,
                'Valid_Loss': avg_eval_loss,
                'Valid_f1_score': avg_f1_score,
                'Valid_EM_score': avg_EM_score,
                'Training_Time': Training_time,
                'Validation_Time': Validation_time
            }
        )

    #Save the training stats
    print("Saving training stats...")
    with open("Training_stats_qa.json", "w") as fh:
        json.dump(training_stats, fh)

    # Save the fine_tuned model
    print("Saving the fine-tuned model...")
    QA_model.save_pretrained('./model/qa_bert_base/')
    print("Training complete!")

#train_and_evaluate_QA_module()





        






